use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, Duration, OffsetDateTime, format_description::well_known::Iso8601};
use uuid::Uuid;

use super::{
    ATTEMPT_LIMIT, FailureReason, GARMIN_KINDS, JobOutcome, JobStoreError, RecordedJobStatus,
    STORAGE_QUEUE_LOCK, begin, fail_exhausted, finish_in_transaction,
};
use crate::{Database, runtime_lease::RuntimeWorkerIdentity};

const KIND: &str = "backup";

#[derive(Clone, Copy)]
pub struct DailyBackupLease {
    id: Uuid,
    token: Uuid,
    seconds: i32,
    date: Date,
}

impl DailyBackupLease {
    pub(crate) async fn check_on(
        &self,
        connection: &mut sqlx::PgConnection,
    ) -> Result<(), JobStoreError> {
        let active: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM jobs WHERE id=$1 AND lease_token=$2
                AND kind='backup' AND status='running' AND lease_until>clock_timestamp())",
        )
        .bind(self.id)
        .bind(self.token)
        .fetch_one(connection)
        .await?;
        active.then_some(()).ok_or(JobStoreError::LeaseLost)
    }

    pub fn date(&self) -> Date {
        self.date
    }

    pub fn lease_duration(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.seconds as u64)
    }
}

impl Database {
    /// Ensure one backup job per UTC day and reopen a failed day after its cooldown.
    pub async fn schedule_daily_backup(
        &self,
        now: StoredInstant,
    ) -> Result<Option<Uuid>, JobStoreError> {
        let at = OffsetDateTime::from(now);
        let date = at.date();
        let sync_wait_until = at
            .checked_add(Duration::minutes(30))
            .and_then(|ts| StoredInstant::try_from(ts).ok())
            .ok_or(JobStoreError::InvalidRecordedState)?;
        let payload =
            json!({"date":date.to_string(),"sync_wait_until":sync_wait_until.to_string()});
        let mut txn = begin(&self.work).await?;
        let scheduled = sqlx::query_scalar(
            "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
              VALUES($1,'backup',$2::jsonb,$3,'pending',$4,0)
             ON CONFLICT(dedup_key) DO UPDATE SET status='pending',attempts=0,
                 payload=excluded.payload,run_at=excluded.run_at,completed_at=NULL,
                 lease_until=NULL,lease_token=NULL,last_error=NULL
             WHERE jobs.kind='backup' AND jobs.status='failed'
               AND coalesce(jobs.completed_at,jobs.run_at)<=$4::timestamptz-interval '1 hour'
             RETURNING id",
        )
        .bind(Uuid::new_v4())
        .bind(payload.to_string())
        .bind(format!("backup:{date}"))
        .bind(at)
        .fetch_optional(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(scheduled)
    }

    /// Admit one scheduled backup using the same fence and wait window as Garmin work.
    pub async fn claim_daily_backup(
        &self,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<DailyBackupLease>, JobStoreError> {
        if !(1..=86_400).contains(&lease_seconds) {
            return Err(JobStoreError::InvalidLeaseDuration);
        }
        let mut txn = begin(&self.work).await?;
        let admitted: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(STORAGE_QUEUE_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !admitted {
            return Ok(None);
        }
        let at: OffsetDateTime = now.into();
        fail_exhausted(&mut txn, at, Some(&[KIND])).await?;
        let token = Uuid::new_v4();
        let seconds = lease_seconds as i32;
        let candidate: Option<(Uuid, String, String, OffsetDateTime)> = sqlx::query_as(
            "WITH eligible AS (
                SELECT job.id FROM jobs job
                WHERE job.kind='backup' AND job.attempts<$1
                  AND ((job.status='pending' AND job.run_at<=$2)
                    OR (job.status='running' AND job.lease_until<$2))
                  AND NOT EXISTS(
                      SELECT 1 FROM jobs active WHERE active.id<>job.id
                        AND active.kind IN ('backup','storage_check') AND active.status='running'
                        AND active.lease_until>=$2
                  )
                  AND NOT EXISTS(
                      SELECT 1 FROM jobs sync WHERE sync.kind=ANY($4::text[])
                        AND sync.status IN ('pending','running')
                        AND (coalesce((job.payload->>'sync_wait_until')::timestamptz,
                                      job.run_at+interval '30 minutes')>$2
                          OR (sync.status='running' AND sync.lease_until>=$2))
                  )
                ORDER BY job.run_at,job.id FOR UPDATE OF job SKIP LOCKED LIMIT 1
             )
             UPDATE jobs job SET status='running',attempts=attempts+1,
                    lease_until=$2::timestamptz+$5*interval '1 second',lease_token=$3
             FROM eligible WHERE job.id=eligible.id
             RETURNING job.id,job.payload::text,job.dedup_key,job.run_at",
        )
        .bind(ATTEMPT_LIMIT)
        .bind(at)
        .bind(token)
        .bind(GARMIN_KINDS)
        .bind(seconds)
        .fetch_optional(&mut *txn)
        .await?;
        let lease = candidate
            .map(|(id, raw, key, run_at)| -> Result<_, JobStoreError> {
                Ok(DailyBackupLease {
                    id,
                    token,
                    seconds,
                    date: backup_date(&raw, &key, run_at)?,
                })
            })
            .transpose()?;
        txn.commit().await?;
        Ok(lease)
    }

    pub async fn renew_daily_backup(
        &self,
        lease: &DailyBackupLease,
        now: StoredInstant,
    ) -> Result<bool, JobStoreError> {
        self.renew_job(lease.id, lease.token, lease.seconds, KIND, now)
            .await
    }

    pub async fn defer_failed_daily_backup(
        &self,
        lease: &DailyBackupLease,
        now: StoredInstant,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        self.finish_job(
            lease.id,
            lease.token,
            KIND,
            now,
            JobOutcome::Failed {
                reason: FailureReason::RuntimeError,
                retry_at: None,
            },
        )
        .await
    }

    /// Record the published bundle and finish the job together under the live lease.
    pub async fn complete_daily_backup(
        &self,
        lease: &DailyBackupLease,
        destination: &str,
        now: StoredInstant,
    ) -> Result<(), JobStoreError> {
        self.complete_backup(lease, destination, now, None).await
    }

    pub async fn complete_worker_backup(
        &self,
        lease: &DailyBackupLease,
        destination: &str,
        now: StoredInstant,
        worker: RuntimeWorkerIdentity,
    ) -> Result<(), JobStoreError> {
        self.complete_backup(lease, destination, now, Some(worker))
            .await
    }

    async fn complete_backup(
        &self,
        lease: &DailyBackupLease,
        destination: &str,
        now: StoredInstant,
        worker: Option<RuntimeWorkerIdentity>,
    ) -> Result<(), JobStoreError> {
        if destination.is_empty() || destination.contains('\0') {
            return Err(JobStoreError::InvalidRecordedState);
        }
        let mut txn = begin(&self.work).await?;
        if let Some(worker) = worker
            && !worker.still_held(&mut txn).await?
        {
            return Err(JobStoreError::LeaseLost);
        }
        complete(&mut txn, lease, destination, now).await?;
        txn.commit().await?;
        Ok(())
    }
}

fn backup_date(raw: &str, key: &str, run_at: OffsetDateTime) -> Result<Date, JobStoreError> {
    let payload: Value =
        serde_json::from_str(raw).map_err(|_| JobStoreError::InvalidRecordedState)?;
    match payload.get("date") {
        Some(Value::String(value)) if !value.is_empty() => {
            Date::parse(value, &Iso8601::DATE).map_err(|_| JobStoreError::InvalidRecordedState)
        }
        None | Some(Value::Null) => Ok(key
            .strip_prefix("backup:")
            .and_then(|value| Date::parse(value, &Iso8601::DATE).ok())
            .unwrap_or_else(|| run_at.date())),
        Some(Value::String(_)) => Ok(run_at.date()),
        _ => Err(JobStoreError::InvalidRecordedState),
    }
}

async fn complete(
    txn: &mut Transaction<'_, Postgres>,
    lease: &DailyBackupLease,
    destination: &str,
    now: StoredInstant,
) -> Result<(), JobStoreError> {
    finish_in_transaction(
        txn,
        lease.id,
        lease.token,
        KIND,
        None,
        JobOutcome::Completed,
    )
    .await?;
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('backup:last_success',$1::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp()",
    )
    .bind(json!({"at":now.to_string(),"path":destination}).to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}
