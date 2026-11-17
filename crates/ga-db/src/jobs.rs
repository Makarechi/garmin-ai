use std::{error::Error, fmt};

use ga_domain::timestamp::StoredInstant;
use sqlx::{PgPool, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness};

const ATTEMPT_LIMIT: i32 = 8;
const KIND: &str = "channel_initiatives";

#[derive(Debug)]
pub enum JobStoreError {
    Storage(ReadinessError),
    InvalidLeaseDuration,
    LeaseLost,
    Database,
}

impl fmt::Display for JobStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Storage(_) => "Job storage is unavailable",
            Self::InvalidLeaseDuration => "Lease duration must be between one second and one day",
            Self::LeaseLost => "Job lease expired or belongs to a different worker",
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
}

impl FailureReason {
    fn label(self) -> &'static str {
        match self {
            Self::DiaryDeferred => "DiaryDeferred",
            Self::ProviderCooldown => "ProviderCooldown",
            Self::RuntimeError => "RuntimeError",
            Self::ProviderRequestInvalid => "ProviderRequestInvalid",
        }
    }

    fn refund(self) -> i32 {
        i32::from(matches!(self, Self::DiaryDeferred | Self::ProviderCooldown))
    }
}

#[derive(Clone, Copy)]
pub enum InitiativeOutcome {
    Completed,
    Failed {
        reason: FailureReason,
        retry_at: Option<StoredInstant>,
    },
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
        fail_exhausted(&mut txn, now).await?;
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

    pub async fn renew_initiative(
        &self,
        lease: &InitiativeLease,
        now: StoredInstant,
    ) -> Result<bool, JobStoreError> {
        let mut txn = begin(&self.control).await?;
        let refreshed = sqlx::query(
            "UPDATE jobs
             SET lease_until = GREATEST(lease_until, $3 + $4 * INTERVAL '1 second')
             WHERE id = $1 AND lease_token = $2 AND status = 'running'
               AND lease_until > $3 AND kind = $5",
        )
        .bind(lease.id)
        .bind(lease.token)
        .bind(OffsetDateTime::from(now))
        .bind(lease.seconds)
        .bind(KIND)
        .execute(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(refreshed.rows_affected() == 1)
    }

    pub async fn finish_initiative(
        &self,
        lease: &InitiativeLease,
        now: StoredInstant,
        outcome: InitiativeOutcome,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        let (error, retry, refund, terminal) = match outcome {
            InitiativeOutcome::Completed => (None, None, 0, false),
            InitiativeOutcome::Failed { reason, retry_at } => (
                Some(reason.label()),
                retry_at.map(OffsetDateTime::from),
                reason.refund(),
                matches!(reason, FailureReason::ProviderRequestInvalid),
            ),
        };
        let mut txn = begin(&self.control).await?;
        let status = sqlx::query_scalar::<_, String>(
            "WITH owned AS (
                 SELECT id,
                    GREATEST(0, attempts - $6) AS effective_attempts,
                    CASE
                       WHEN $5::text IS NULL THEN 'done'
                       WHEN $9 OR attempts - $6 >= $7 THEN 'failed'
                       ELSE 'pending'
                    END AS final_status
                 FROM jobs
                 WHERE id = $1 AND lease_token = $2 AND status = 'running'
                   AND lease_until > $3 AND kind = $8
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
                                  WHEN $4::timestamptz IS NOT NULL THEN GREATEST($3, $4)
                                  ELSE $3 + (LEAST(3600, 15 * (2 ^ owned.effective_attempts))
                                             + 10 * random()) * INTERVAL '1 second'
                              END
                              WHEN 'failed' THEN $3
                              ELSE jobs.run_at
                          END,
                 completed_at = CASE WHEN owned.final_status != 'pending' THEN $3 ELSE NULL END
             FROM owned WHERE jobs.id = owned.id RETURNING jobs.status",
        )
        .bind(lease.id)
        .bind(lease.token)
        .bind(OffsetDateTime::from(now))
        .bind(retry)
        .bind(error)
        .bind(refund)
        .bind(ATTEMPT_LIMIT)
        .bind(KIND)
        .bind(terminal)
        .fetch_optional(&mut *txn)
        .await?;
        txn.commit().await?;
        match status.as_deref() {
            Some("done") => Ok(RecordedJobStatus::Completed),
            Some("pending") => Ok(RecordedJobStatus::Deferred),
            Some("failed") => Ok(RecordedJobStatus::Failed),
            None => Err(JobStoreError::LeaseLost),
            Some(_) => Err(JobStoreError::Database),
        }
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
) -> Result<(), JobStoreError> {
    sqlx::query(
        "WITH exhausted AS (
            SELECT id FROM jobs
            WHERE kind = $1 AND attempts >= $2
              AND (status = 'pending' OR (status = 'running' AND lease_until < $3))
            ORDER BY run_at, id
            FOR UPDATE SKIP LOCKED LIMIT 100
         )
         UPDATE jobs SET status = 'failed', last_error = 'RetryLimitExceeded',
            completed_at = $3, lease_until = NULL, lease_token = NULL
         FROM exhausted WHERE jobs.id = exhausted.id",
    )
    .bind(KIND)
    .bind(ATTEMPT_LIMIT)
    .bind(now)
    .execute(&mut **txn)
    .await?;
    Ok(())
}
