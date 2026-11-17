use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use sqlx::{PgConnection, PgPool, Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness};

const ATTEMPT_LIMIT: i32 = 8;
const KIND: &str = "channel_initiatives";
pub(crate) const STORAGE_QUEUE_LOCK: i64 = 72104624;
const GARMIN_KINDS: &[&str] = &["garmin_endpoint", "garmin_activities", "garmin_fit"];

#[derive(Debug)]
pub enum JobStoreError {
    Storage(ReadinessError),
    InvalidLeaseDuration,
    LeaseLost,
    PublicationTimedOut,
    InvalidRecordedState,
    Database,
}

impl fmt::Display for JobStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Storage(_) => "Job storage is unavailable",
            Self::InvalidLeaseDuration => "Lease duration must be between one second and one day",
            Self::LeaseLost => "Job lease expired or belongs to a different worker",
            Self::PublicationTimedOut => "Job publication timed out",
            Self::InvalidRecordedState => "Recorded job state is invalid",
            Self::Database => "Job database operation failed",
        })
    }
}

impl Error for JobStoreError {}

impl From<sqlx::Error> for JobStoreError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

pub struct InitiativeLease {
    id: Uuid,
    token: Uuid,
    seconds: i32,
    attempt: i32,
}

impl InitiativeLease {
    pub fn id(&self) -> Uuid {
        self.id
    }

    pub fn attempt(&self) -> i32 {
        self.attempt
    }
}

#[derive(Clone, Copy)]
pub enum FailureReason {
    DiaryDeferred,
    ProviderCooldown,
    RuntimeError,
    ProviderRequestInvalid,
    GarminCollectionDisabled,
    GarminAuthenticationRequired,
    InvalidGarminWork,
}

impl FailureReason {
    fn label(self) -> &'static str {
        match self {
            Self::DiaryDeferred => "DiaryDeferred",
            Self::ProviderCooldown => "ProviderCooldown",
            Self::RuntimeError => "RuntimeError",
            Self::ProviderRequestInvalid => "ProviderRequestInvalid",
            Self::GarminCollectionDisabled => "GarminCollectionDisabled",
            Self::GarminAuthenticationRequired => "AuthenticationRequired",
            Self::InvalidGarminWork => "ValueError",
        }
    }

    fn refund(self) -> i32 {
        i32::from(matches!(self, Self::DiaryDeferred | Self::ProviderCooldown))
    }
}

#[derive(Clone, Copy)]
pub enum JobOutcome {
    Completed,
    Failed {
        reason: FailureReason,
        retry_at: Option<StoredInstant>,
    },
}

pub type InitiativeOutcome = JobOutcome;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GarminJobKind {
    Endpoint,
    Activities,
    Fit,
}

impl GarminJobKind {
    fn stored(value: &str) -> Result<Self, JobStoreError> {
        match value {
            "garmin_endpoint" => Ok(Self::Endpoint),
            "garmin_activities" => Ok(Self::Activities),
            "garmin_fit" => Ok(Self::Fit),
            _ => Err(JobStoreError::InvalidRecordedState),
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Endpoint => "garmin_endpoint",
            Self::Activities => "garmin_activities",
            Self::Fit => "garmin_fit",
        }
    }
}

pub struct GarminJobLease {
    id: Uuid,
    token: Uuid,
    seconds: i32,
    attempt: i32,
    kind: GarminJobKind,
    payload: String,
}

impl GarminJobLease {
    pub fn id(&self) -> Uuid {
        self.id
    }

    pub fn attempt(&self) -> i32 {
        self.attempt
    }

    pub fn kind(&self) -> GarminJobKind {
        self.kind
    }

    pub fn payload_json(&self) -> &str {
        &self.payload
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RecordedJobStatus {
    Completed,
    Deferred,
    Failed,
}

impl Database {
    pub async fn enqueue_initiative(
        &self,
        dedup_key: &str,
        run_at: StoredInstant,
    ) -> Result<Option<Uuid>, JobStoreError> {
        let mut txn = begin(&self.work).await?;
        let id = sqlx::query_scalar(
            "INSERT INTO jobs (id, kind, payload, dedup_key, status, run_at, attempts)
             VALUES ($1, $2, '{}'::jsonb, $3, 'pending', $4, 0)
             ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
        )
        .bind(Uuid::new_v4())
        .bind(KIND)
        .bind(dedup_key)
        .bind(OffsetDateTime::from(run_at))
        .fetch_optional(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(id)
    }

    /// Reserve one due job in the local channel initiative lane.
    pub async fn claim_local_initiative(
        &self,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<InitiativeLease>, JobStoreError> {
        if !(1..=86_400).contains(&lease_seconds) {
            return Err(JobStoreError::InvalidLeaseDuration);
        }
        let seconds = lease_seconds as i32;
        let now = OffsetDateTime::from(now);
        let mut txn = begin(&self.work).await?;
        fail_exhausted(&mut txn, now, Some(&[KIND])).await?;
        let token = Uuid::new_v4();
        let row = sqlx::query_as::<_, (Uuid, i32)>(
            "WITH eligible AS (
                 SELECT id FROM jobs
                 WHERE kind = $1 AND attempts < $2
                   AND ((status = 'pending' AND run_at <= $3)
                        OR (status = 'running' AND lease_until < $3))
                 ORDER BY run_at, id
                 FOR UPDATE SKIP LOCKED LIMIT 1
             )
             UPDATE jobs SET
                 status = 'running',
                 lease_until = $3 + $4 * INTERVAL '1 second',
                 lease_token = $5,
                 attempts = attempts + 1
             FROM eligible WHERE jobs.id = eligible.id
             RETURNING jobs.id, jobs.attempts",
        )
        .bind(KIND)
        .bind(ATTEMPT_LIMIT)
        .bind(now)
        .bind(seconds)
        .bind(token)
        .fetch_optional(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(row.map(|(id, attempt)| InitiativeLease {
            id,
            token,
            seconds,
            attempt,
        }))
    }

    /// Claim the next existing Garmin job with the baseline storage and pacing gates.
    pub async fn claim_garmin_job(
        &self,
        now: StoredInstant,
        owner_local_date: Date,
        lease_seconds: u32,
        backups_enabled: bool,
    ) -> Result<Option<GarminJobLease>, JobStoreError> {
        if !(1..=86_400).contains(&lease_seconds) {
            return Err(JobStoreError::InvalidLeaseDuration);
        }
        let seconds = lease_seconds as i32;
        let mut txn = begin(&self.work).await?;
        let admitted: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(STORAGE_QUEUE_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !admitted {
            return Ok(None);
        }
        fail_exhausted(&mut txn, now.into(), None).await?;
        if garmin_paused(&mut txn, now).await? {
            txn.commit().await?;
            return Ok(None);
        }
        let token = Uuid::new_v4();
        let claimed = sqlx::query_as::<_, (Uuid, i32, String, String)>(
            "WITH day AS (
                SELECT to_char($6::date, 'YYYY-MM-DD') AS today,
                       to_char($6::date - 1, 'YYYY-MM-DD') AS yesterday,
                       to_char($6::date - 30, 'YYYY-MM-DD') AS recent
             ), eligible AS (
                SELECT job.id FROM jobs AS job CROSS JOIN day
                WHERE job.kind = ANY($1::text[]) AND job.attempts < $2
                  AND ((job.status = 'pending' AND job.run_at <= $3)
                    OR (job.status = 'running' AND job.lease_until < $3))
                  AND (NOT $7 OR NOT EXISTS (
                    SELECT 1 FROM jobs AS backup
                    WHERE backup.kind = 'backup' AND (
                        backup.status = 'running'
                        OR (backup.status = 'pending' AND backup.attempts < $2 AND backup.run_at <= $3
                           AND coalesce(
                               (backup.payload->>'sync_wait_until')::timestamptz,
                               backup.run_at + interval '30 minutes'
                           ) <= $3)
                    )
                  ))
                ORDER BY CASE
                  WHEN job.payload->'backfill' IS DISTINCT FROM 'true'::jsonb
                      AND ((job.kind = 'garmin_endpoint' AND job.payload->>'key' = day.today
                           AND job.payload->>'endpoint' IN ('heart_rate', 'stress'))
                        OR (job.run_at >= $3 - interval '1 day'
                           AND (job.kind = 'garmin_activities'
                             OR (job.kind = 'garmin_endpoint' AND left(job.dedup_key, 9) = 'activity:')
                             OR (job.kind = 'garmin_fit' AND left(job.dedup_key, 4) = 'fit:'))))
                    THEN 0
                  WHEN job.kind = 'garmin_endpoint' AND job.payload->>'endpoint' = 'stress'
                    AND job.payload->>'key' >= day.recent AND job.payload->>'key' < day.today
                    THEN 1
                  WHEN job.kind = 'garmin_endpoint' AND job.payload->'backfill' IS DISTINCT FROM 'true'::jsonb
                    AND job.payload->>'key' = day.today THEN 2
                  WHEN job.kind = 'garmin_endpoint' AND job.payload->>'endpoint' = 'heart_rate'
                    AND job.payload->>'key' >= day.recent AND job.payload->>'key' < day.today
                    THEN 3
                  WHEN job.kind = 'garmin_endpoint' AND job.payload->'backfill' IS DISTINCT FROM 'true'::jsonb
                    AND job.payload->>'key' = day.yesterday THEN 4
                  ELSE 5 END,
                  CASE WHEN job.kind = 'garmin_endpoint' AND job.payload->>'endpoint' IN ('heart_rate', 'stress')
                    AND job.payload->>'key' >= day.recent AND job.payload->>'key' < day.today
                    THEN job.payload->>'key' ELSE '' END DESC,
                  (job.payload->'backfill' IS NOT DISTINCT FROM 'true'::jsonb), job.run_at, job.id
                FOR UPDATE OF job SKIP LOCKED LIMIT 1
             )
             UPDATE jobs SET status = 'running',
                 lease_until = $3 + $4 * interval '1 second', lease_token = $5,
                 attempts = attempts + 1
             FROM eligible WHERE jobs.id = eligible.id
             RETURNING jobs.id, jobs.attempts, jobs.kind, jobs.payload::text",
        )
        .bind(GARMIN_KINDS)
        .bind(ATTEMPT_LIMIT)
        .bind(OffsetDateTime::from(now))
        .bind(seconds)
        .bind(token)
        .bind(owner_local_date)
        .bind(backups_enabled)
        .fetch_optional(&mut *txn)
        .await?;
        let lease = match claimed {
            Some((id, attempt, kind, payload)) => Some(GarminJobLease {
                id,
                token,
                seconds,
                attempt,
                kind: GarminJobKind::stored(&kind)?,
                payload,
            }),
            None => None,
        };
        txn.commit().await?;
        Ok(lease)
    }

    pub async fn renew_initiative(
        &self,
        lease: &InitiativeLease,
        now: StoredInstant,
    ) -> Result<bool, JobStoreError> {
        self.renew_job(lease.id, lease.token, lease.seconds, KIND, now)
            .await
    }

    pub async fn renew_garmin_job(
        &self,
        lease: &GarminJobLease,
        now: StoredInstant,
    ) -> Result<bool, JobStoreError> {
        self.renew_job(
            lease.id,
            lease.token,
            lease.seconds,
            lease.kind.label(),
            now,
        )
        .await
    }

    async fn renew_job(
        &self,
        id: Uuid,
        token: Uuid,
        seconds: i32,
        kind: &str,
        now: StoredInstant,
    ) -> Result<bool, JobStoreError> {
        let mut txn = begin(&self.control).await?;
        let refreshed = sqlx::query(
            "UPDATE jobs
             SET lease_until = GREATEST(lease_until, $3 + $4 * INTERVAL '1 second')
             WHERE id = $1 AND lease_token = $2 AND status = 'running'
               AND lease_until > $3 AND kind = $5",
        )
        .bind(id)
        .bind(token)
        .bind(OffsetDateTime::from(now))
        .bind(seconds)
        .bind(kind)
        .execute(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(refreshed.rows_affected() == 1)
    }

    /// Apply local database effects and record completion in the same transaction.
    /// The callback receives the fenced connection and is only for database work.
    pub async fn publish_initiative(
        &self,
        lease: &InitiativeLease,
        apply: impl AsyncFnOnce(&mut PgConnection) -> Result<(), JobStoreError>,
    ) -> Result<(), JobStoreError> {
        let mut txn = begin(&self.work).await?;
        let owned = sqlx::query_scalar::<_, i32>(
            "SELECT 1 FROM jobs WHERE id = $1 AND lease_token = $2
               AND status = 'running' AND lease_until > clock_timestamp() AND kind = $3
             FOR UPDATE NOWAIT",
        )
        .bind(lease.id)
        .bind(lease.token)
        .bind(KIND)
        .fetch_optional(&mut *txn)
        .await?;
        if owned.is_none() {
            return Err(JobStoreError::LeaseLost);
        }

        tokio::time::timeout(Duration::from_secs(2), apply(&mut txn))
            .await
            .map_err(|_| JobStoreError::PublicationTimedOut)??;
        let completed = sqlx::query(
            "WITH instant AS MATERIALIZED (SELECT clock_timestamp() AS now)
             UPDATE jobs SET status = 'done', last_error = NULL,
                             lease_until = NULL, lease_token = NULL, completed_at = instant.now
             FROM instant WHERE id = $1 AND lease_token = $2 AND status = 'running'
                          AND lease_until > instant.now AND kind = $3",
        )
        .bind(lease.id)
        .bind(lease.token)
        .bind(KIND)
        .execute(&mut *txn)
        .await?;
        if completed.rows_affected() != 1 {
            return Err(JobStoreError::LeaseLost);
        }
        txn.commit().await?;
        Ok(())
    }

    pub async fn finish_initiative(
        &self,
        lease: &InitiativeLease,
        now: StoredInstant,
        outcome: InitiativeOutcome,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        self.finish_job(lease.id, lease.token, KIND, now, outcome)
            .await
    }

    pub async fn finish_garmin_job(
        &self,
        lease: &GarminJobLease,
        now: StoredInstant,
        outcome: JobOutcome,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        self.finish_job(lease.id, lease.token, lease.kind.label(), now, outcome)
            .await
    }

    async fn finish_job(
        &self,
        id: Uuid,
        token: Uuid,
        kind: &str,
        now: StoredInstant,
        outcome: JobOutcome,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        let mut txn = begin(&self.control).await?;
        let status =
            finish_in_transaction(&mut txn, id, token, kind, Some(now.into()), outcome).await?;
        txn.commit().await?;
        Ok(status)
    }
}

impl GarminJobLease {
    pub(crate) async fn check_live(
        &self,
        txn: &mut Transaction<'_, Postgres>,
    ) -> Result<(), JobStoreError> {
        let found = sqlx::query_scalar::<_, i32>(
            "SELECT 1 FROM jobs WHERE id = $1 AND lease_token = $2
               AND status = 'running' AND lease_until > clock_timestamp() AND kind = $3
             FOR UPDATE NOWAIT",
        )
        .bind(self.id)
        .bind(self.token)
        .bind(self.kind.label())
        .fetch_optional(&mut **txn)
        .await?;
        found.map(|_| ()).ok_or(JobStoreError::LeaseLost)
    }

    pub(crate) async fn finish_in(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        outcome: JobOutcome,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        finish_in_transaction(txn, self.id, self.token, self.kind.label(), None, outcome).await
    }
}

async fn finish_in_transaction(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    token: Uuid,
    kind: &str,
    now: Option<OffsetDateTime>,
    outcome: JobOutcome,
) -> Result<RecordedJobStatus, JobStoreError> {
    let (error, retry, refund, terminal) = match outcome {
        JobOutcome::Completed => (None, None, 0, false),
        JobOutcome::Failed { reason, retry_at } => (
            Some(reason.label()),
            retry_at.map(OffsetDateTime::from),
            reason.refund(),
            matches!(reason, FailureReason::ProviderRequestInvalid),
        ),
    };
    let status = sqlx::query_scalar::<_, String>(
            "WITH instant AS MATERIALIZED (SELECT coalesce($3, clock_timestamp()) AS at), owned AS (
                 SELECT id, instant.at,
                    GREATEST(0, attempts - $6) AS effective_attempts,
                    CASE
                       WHEN $5::text IS NULL THEN 'done'
                       WHEN $9 OR attempts - $6 >= $7 THEN 'failed'
                       ELSE 'pending'
                    END AS final_status
                 FROM jobs CROSS JOIN instant
                 WHERE id = $1 AND lease_token = $2 AND status = 'running'
                   AND lease_until > instant.at AND kind = $8
                 FOR UPDATE
             )
             UPDATE jobs SET
                 status = owned.final_status,
                 attempts = owned.effective_attempts,
                 last_error = $5,
                 lease_until = NULL,
                 lease_token = NULL,
                 run_at = CASE owned.final_status
                              WHEN 'pending' THEN CASE
                                  WHEN $4::timestamptz IS NOT NULL THEN GREATEST(owned.at, $4)
                                  ELSE owned.at + (LEAST(3600, 15 * (2 ^ owned.effective_attempts))
                                             + 10 * random()) * INTERVAL '1 second'
                              END
                              WHEN 'failed' THEN owned.at
                              ELSE jobs.run_at
                          END,
                 completed_at = CASE WHEN owned.final_status != 'pending' THEN owned.at ELSE NULL END
             FROM owned WHERE jobs.id = owned.id RETURNING jobs.status",
        )
        .bind(id)
        .bind(token)
        .bind(now)
        .bind(retry)
        .bind(error)
        .bind(refund)
        .bind(ATTEMPT_LIMIT)
        .bind(kind)
        .bind(terminal)
        .fetch_optional(&mut **txn)
        .await?;
    match status.as_deref() {
        Some("done") => Ok(RecordedJobStatus::Completed),
        Some("pending") => Ok(RecordedJobStatus::Deferred),
        Some("failed") => Ok(RecordedJobStatus::Failed),
        None => Err(JobStoreError::LeaseLost),
        Some(_) => Err(JobStoreError::Database),
    }
}

async fn begin(pool: &PgPool) -> Result<Transaction<'_, Postgres>, JobStoreError> {
    let mut txn = pool.begin().await?;
    sqlx::query("SET LOCAL statement_timeout = '750ms'")
        .execute(&mut *txn)
        .await?;
    check_readiness(&mut txn)
        .await
        .map_err(JobStoreError::Storage)?;
    Ok(txn)
}

async fn fail_exhausted(
    txn: &mut Transaction<'_, Postgres>,
    now: OffsetDateTime,
    kinds: Option<&[&str]>,
) -> Result<(), JobStoreError> {
    sqlx::query(
        "WITH exhausted AS (
            SELECT id FROM jobs
            WHERE ($1::text[] IS NULL OR kind = ANY($1::text[])) AND attempts >= $2
              AND (status = 'pending' OR (status = 'running' AND lease_until < $3))
            ORDER BY run_at, id
            FOR UPDATE SKIP LOCKED LIMIT 100
         )
         UPDATE jobs SET status = 'failed', last_error = 'RetryLimitExceeded',
            completed_at = $3, lease_until = NULL, lease_token = NULL
         FROM exhausted WHERE jobs.id = exhausted.id",
    )
    .bind(kinds)
    .bind(ATTEMPT_LIMIT)
    .bind(now)
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn garmin_paused(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
) -> Result<bool, JobStoreError> {
    let row = sqlx::query_as::<_, (Option<String>, Option<String>)>(
        "SELECT value->>'status', nullif(value->>'blocked_until', '') FROM app_state
         WHERE key = 'integration:garmin'",
    )
    .fetch_optional(&mut **txn)
    .await?;
    let Some((status, blocked_until)) = row else {
        return Ok(false);
    };
    if status.as_deref() == Some("reauth_required") {
        return Ok(true);
    }
    let Some(blocked_until) = blocked_until else {
        return Ok(false);
    };
    let blocked: StoredInstant = blocked_until
        .parse()
        .map_err(|_| JobStoreError::InvalidRecordedState)?;
    Ok(blocked > now)
}
