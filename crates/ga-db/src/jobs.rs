mod backup;
pub use backup::DailyBackupLease;
mod storage_check;
pub(crate) mod telegram_notices;
pub use storage_check::{StorageCheckLease, backup_capacity_report};
mod replay;
mod source_plugin;
pub mod telegram_ack;
pub mod telegram_inbound;
pub use source_plugin::SourcePluginLease;

pub use replay::ReplayJobLease;

use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use sqlx::{PgConnection, PgPool, Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness,
    garmin_integration::{GarminIntegrationError, current_source_selected},
};

const ATTEMPT_LIMIT: i32 = 8;
const KIND: &str = "channel_initiatives";
pub(crate) const GARMIN_PUBLICATION_SECONDS: i32 = 30;
pub(crate) const STORAGE_QUEUE_LOCK: i64 = 72_104_624;
const GARMIN_KINDS: &[&str] = &["garmin_endpoint", "garmin_activities", "garmin_fit"];

#[derive(Debug)]
pub enum JobStoreError {
    Storage(ReadinessError),
    Busy,
    InvalidLeaseDuration,
    LeaseLost,
    PublicationTimedOut,
    InvalidRecordedState,
    StatementCanceled,
    Database,
}

impl fmt::Display for JobStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Storage(_) => "Job storage is unavailable",
            Self::Busy => "Job queue is busy",
            Self::InvalidLeaseDuration => "Lease duration must be between one second and one day",
            Self::LeaseLost => "Job lease expired or belongs to a different worker",
            Self::PublicationTimedOut => "Job publication timed out",
            Self::InvalidRecordedState => "Recorded job state is invalid",
            Self::StatementCanceled => "Job database query was canceled",
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
    #[must_use]
    pub fn id(&self) -> Uuid {
        self.id
    }

    #[must_use]
    pub fn attempt(&self) -> i32 {
        self.attempt
    }
}

#[derive(Clone, Copy)]
pub enum FailureReason {
    DiaryDeferred,
    ProviderCooldown,
    RuntimeError,
    ReplayBusy,
    ProviderRequestInvalid,
    TelegramPairingChanged,
    TelegramPolicyBusy,
    GarminCollectionDisabled,
    GarminStorageBusy,
    GarminAuthenticationRequired,
    InvalidGarminWork,
}

impl FailureReason {
    fn label(self) -> &'static str {
        match self {
            Self::DiaryDeferred => "DiaryDeferred",
            Self::ProviderCooldown => "ProviderCooldown",
            Self::RuntimeError => "RuntimeError",
            Self::ReplayBusy => "ReplayBusy",
            Self::ProviderRequestInvalid => "ProviderRequestInvalid",
            Self::TelegramPairingChanged => "TelegramPairingChanged",
            Self::TelegramPolicyBusy => "TelegramPolicyBusy",
            Self::GarminCollectionDisabled => "GarminCollectionDisabled",
            Self::GarminStorageBusy => "GarminStorageBusy",
            Self::GarminAuthenticationRequired => "AuthenticationRequired",
            Self::InvalidGarminWork => "ValueError",
        }
    }

    fn refund(self) -> i32 {
        i32::from(matches!(
            self,
            Self::DiaryDeferred
                | Self::ProviderCooldown
                | Self::TelegramPolicyBusy
                | Self::ReplayBusy
                | Self::GarminStorageBusy
        ))
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

    #[must_use]
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
    source_instance: Option<String>,
}

impl GarminJobLease {
    #[must_use]
    pub fn id(&self) -> Uuid {
        self.id
    }

    #[must_use]
    pub fn attempt(&self) -> i32 {
        self.attempt
    }

    #[must_use]
    pub fn kind(&self) -> GarminJobKind {
        self.kind
    }

    #[must_use]
    pub fn lease_duration(&self) -> std::time::Duration {
        std::time::Duration::from_secs(u64::try_from(self.seconds).unwrap_or_default())
    }

    #[must_use]
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
        let seconds = lease_seconds_for_sql(lease_seconds)?;
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
        self.claim_garmin_job_in_scope(now, owner_local_date, lease_seconds, backups_enabled, None)
            .await
    }

    /// Bind new runtime work to its selected source through renewal and publication.
    pub async fn claim_selected_garmin_job(
        &self,
        now: StoredInstant,
        owner_local_date: Date,
        lease_seconds: u32,
        backups_enabled: bool,
        source_instance: &str,
    ) -> Result<Option<GarminJobLease>, JobStoreError> {
        self.claim_garmin_job_in_scope(
            now,
            owner_local_date,
            lease_seconds,
            backups_enabled,
            Some(source_instance),
        )
        .await
    }

    async fn claim_garmin_job_in_scope(
        &self,
        now: StoredInstant,
        owner_local_date: Date,
        lease_seconds: u32,
        backups_enabled: bool,
        source_instance: Option<&str>,
    ) -> Result<Option<GarminJobLease>, JobStoreError> {
        let seconds = lease_seconds_for_sql(lease_seconds)?;
        let mut txn = begin(&self.work).await?;
        if let Some(instance) = source_instance {
            match current_source_selected(&mut txn, instance).await {
                Ok(true) => {}
                Ok(false) => return Ok(None),
                Err(GarminIntegrationError::Busy) => return Err(JobStoreError::Busy),
                Err(_) => return Err(JobStoreError::Database),
            }
        }
        let admitted: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(STORAGE_QUEUE_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !admitted {
            return Err(JobStoreError::Busy);
        }
        fail_exhausted(&mut txn, now.into(), None).await?;
        let paused = crate::garmin_integration::garmin_pause(&mut *txn, now)
            .await
            .map_err(|error| match error {
                GarminIntegrationError::InvalidState => JobStoreError::InvalidRecordedState,
                _ => JobStoreError::Database,
            })?;
        if paused.is_some() {
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
                  WHEN job.kind='garmin_endpoint' AND starts_with(job.dedup_key,'telegram-refresh:')
                       AND job.payload->>'key'=day.today THEN -1
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
                source_instance: source_instance.map(str::to_owned),
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
        self.renew_job(lease.id, lease.token, lease.seconds, KIND, now, None)
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
            lease.source_instance.as_deref(),
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
        source_instance: Option<&str>,
    ) -> Result<bool, JobStoreError> {
        let mut txn = begin(&self.control).await?;
        if !source_allows_job(&mut txn, source_instance).await? {
            return Ok(false);
        }
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

async fn source_allows_job(
    txn: &mut Transaction<'_, Postgres>,
    instance: Option<&str>,
) -> Result<bool, JobStoreError> {
    match instance {
        Some(instance) => current_source_selected(txn, instance)
            .await
            .map_err(|_| JobStoreError::Database),
        None => Ok(true),
    }
}

impl GarminJobLease {
    pub(crate) async fn observe_live(
        &self,
        txn: &mut Transaction<'_, Postgres>,
    ) -> Result<(), JobStoreError> {
        self.require_live(txn, false).await
    }

    pub(crate) async fn check_live(
        &self,
        txn: &mut Transaction<'_, Postgres>,
    ) -> Result<(), JobStoreError> {
        self.require_live(txn, true).await
    }

    pub(crate) async fn reserve_publication(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        seconds: i32,
    ) -> Result<(), JobStoreError> {
        let refreshed = sqlx::query(
            "WITH instant AS MATERIALIZED (SELECT clock_timestamp() AS at)
             UPDATE jobs SET lease_until = GREATEST(lease_until, instant.at + $4 * INTERVAL '1 second')
             FROM instant WHERE id = $1 AND lease_token = $2 AND kind = $3
               AND status = 'running' AND lease_until > instant.at",
        )
        .bind(self.id)
        .bind(self.token)
        .bind(self.kind.label())
        .bind(seconds)
        .execute(&mut **txn)
        .await?;
        if refreshed.rows_affected() != 1 {
            return Err(JobStoreError::LeaseLost);
        }
        Ok(())
    }

    async fn require_live(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        lock: bool,
    ) -> Result<(), JobStoreError> {
        if !source_allows_job(txn, self.source_instance.as_deref()).await? {
            return Err(JobStoreError::LeaseLost);
        }
        macro_rules! live {
            ($suffix:literal) => {
                concat!(
                    "SELECT 1 FROM jobs WHERE id = $1 AND lease_token = $2
                     AND status = 'running' AND lease_until > clock_timestamp() AND kind = $3",
                    $suffix
                )
            };
        }
        let query = if lock {
            live!(" FOR UPDATE NOWAIT")
        } else {
            live!("")
        };
        let found = sqlx::query_scalar::<_, i32>(query)
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
            matches!(
                reason,
                FailureReason::ProviderRequestInvalid | FailureReason::TelegramPairingChanged
            ),
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

fn lease_seconds_for_sql(seconds: u32) -> Result<i32, JobStoreError> {
    i32::try_from(seconds)
        .ok()
        .filter(|seconds| (1..=86_400).contains(seconds))
        .ok_or(JobStoreError::InvalidLeaseDuration)
}
