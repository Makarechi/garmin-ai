use ga_archive::backup_space::{BackupSpace, RESERVE_BYTES};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ATTEMPT_LIMIT, FailureReason, JobOutcome, JobStoreError, STORAGE_QUEUE_LOCK, begin,
    fail_exhausted, finish_in_transaction,
};
use crate::{Database, runtime_lease::RuntimeWorkerIdentity};

const KIND: &str = "storage_check";
const SIX_HOURS: i64 = 6 * 60 * 60;

pub struct StorageCheckLease {
    id: Uuid,
    token: Uuid,
    seconds: i32,
}

impl StorageCheckLease {
    pub fn lease_duration(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.seconds as u64)
    }
}

/// The CLI and the worker expose the same sanitized estimate.
pub fn backup_capacity_report(estimate: &BackupSpace) -> Value {
    let volumes: Vec<_> = estimate
        .volumes
        .iter()
        .map(|volume| {
            json!({
                "role":volume.role.label(),
                "free_bytes":volume.free_bytes,
                "required_bytes":volume.required_bytes,
            })
        })
        .collect();
    json!({
        "status":if estimate.ready() {"ready"} else {"insufficient"},
        "estimate_only":true, "reserve_bytes":RESERVE_BYTES, "volumes":volumes,
    })
}

impl Database {
    /// Schedule the capacity estimate once per UTC six-hour interval.
    pub async fn schedule_storage_check(&self, now: StoredInstant) -> Result<(), JobStoreError> {
        let at = OffsetDateTime::from(now);
        let key = format!(
            "storage-check:{}",
            at.unix_timestamp().div_euclid(SIX_HOURS)
        );
        let mut txn = begin(&self.work).await?;
        sqlx::query(
            "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
              VALUES($1,$2,'{}'::jsonb,$3,'pending',$4,0) ON CONFLICT(dedup_key) DO NOTHING",
        )
        .bind(Uuid::new_v4())
        .bind(KIND)
        .bind(key)
        .bind(at)
        .execute(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(())
    }

    /// Exclude another storage job throughout the estimate's active lease.
    pub async fn claim_storage_check(
        &self,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<StorageCheckLease>, JobStoreError> {
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
        let at = OffsetDateTime::from(now);
        fail_exhausted(&mut txn, at, Some(&[KIND])).await?;
        let token = Uuid::new_v4();
        let seconds = lease_seconds as i32;
        let id: Option<Uuid> = sqlx::query_scalar(
            "WITH eligible AS (
                SELECT job.id FROM jobs job WHERE job.kind=$1 AND job.attempts<$2
                  AND ((job.status='pending' AND job.run_at<=$3)
                    OR (job.status='running' AND job.lease_until<$3))
                  AND NOT EXISTS(
                      SELECT 1 FROM jobs active WHERE active.id<>job.id
                        AND active.kind IN ('backup','storage_check') AND active.status='running'
                        AND active.lease_until>=$3
                  )
                ORDER BY job.run_at, job.id FOR UPDATE OF job SKIP LOCKED LIMIT 1
             )
             UPDATE jobs job SET status='running',attempts=attempts+1,
                    lease_until=$3::timestamptz+$4*interval '1 second',lease_token=$5
             FROM eligible WHERE job.id=eligible.id RETURNING job.id",
        )
        .bind(KIND)
        .bind(ATTEMPT_LIMIT)
        .bind(at)
        .bind(seconds)
        .bind(token)
        .fetch_optional(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(id.map(|id| StorageCheckLease { id, token, seconds }))
    }

    pub async fn renew_storage_check(
        &self,
        lease: &StorageCheckLease,
        now: StoredInstant,
    ) -> Result<bool, JobStoreError> {
        self.renew_job(lease.id, lease.token, lease.seconds, KIND, now)
            .await
    }

    /// Record the sanitized estimate and complete its lease together.
    pub async fn complete_storage_check(
        &self,
        lease: &StorageCheckLease,
        estimate: BackupSpace,
        observed_at: StoredInstant,
        worker: RuntimeWorkerIdentity,
    ) -> Result<(), JobStoreError> {
        let mut report = backup_capacity_report(&estimate);
        report["at"] = Value::String(observed_at.to_string());
        let mut txn = begin(&self.work).await?;
        if !worker.still_held(&mut txn).await? {
            return Err(JobStoreError::LeaseLost);
        }
        finish_in_transaction(
            &mut txn,
            lease.id,
            lease.token,
            KIND,
            None,
            JobOutcome::Completed,
        )
        .await?;
        sqlx::query(
            "INSERT INTO app_state(key,value) VALUES('storage:backup-capacity',$1::jsonb)
             ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp()",
        )
        .bind(report.to_string())
        .execute(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(())
    }

    pub async fn defer_storage_check(
        &self,
        lease: &StorageCheckLease,
        now: StoredInstant,
    ) -> Result<(), JobStoreError> {
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
        .await?;
        Ok(())
    }
}
