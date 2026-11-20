use ga_domain::timestamp::StoredInstant;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ATTEMPT_LIMIT, FailureReason, JobOutcome, JobStoreError, RecordedJobStatus, STORAGE_QUEUE_LOCK,
    begin, finish_in_transaction,
};
use crate::{
    Database,
    activity_documents::{ActivityDocument, ActivitySummaryKind},
    fit_ingest::ENDPOINT as FIT_ENDPOINT,
    history_windows::DAY_ENDPOINTS,
    replay::{GARMIN_PARSER_VERSION, HEALTH_CALENDAR_ENDPOINTS, HEALTH_INTRADAY_ENDPOINTS},
};

const KIND: &str = "raw_replay";

pub struct ReplayJobLease {
    id: Uuid,
    token: Uuid,
    seconds: i32,
    attempt: i32,
    payload: String,
    endpoint: String,
}

impl ReplayJobLease {
    pub fn id(&self) -> Uuid {
        self.id
    }

    pub fn attempt(&self) -> i32 {
        self.attempt
    }

    pub(crate) fn endpoint(&self) -> &str {
        &self.endpoint
    }

    pub fn lease_duration(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.seconds as u64)
    }

    pub(crate) fn source(&self) -> Option<(Uuid, String)> {
        let payload: Value = serde_json::from_str(&self.payload).ok()?;
        let id = payload["raw_ref"].as_str()?.parse().ok()?;
        let account = payload["account"].as_str()?.to_owned();
        Some((id, account))
    }

    pub(crate) async fn assert_live(
        &self,
        txn: &mut Transaction<'_, Postgres>,
    ) -> Result<(), JobStoreError> {
        let live: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM jobs WHERE id=$1 AND lease_token=$2
                   AND kind='raw_replay' AND status='running' AND lease_until>clock_timestamp())",
        )
        .bind(self.id)
        .bind(self.token)
        .fetch_one(&mut **txn)
        .await?;
        if live {
            Ok(())
        } else {
            Err(JobStoreError::LeaseLost)
        }
    }

    pub(crate) async fn finish_in(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        outcome: JobOutcome,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        finish_in_transaction(txn, self.id, self.token, KIND, None, outcome).await
    }
}

impl Database {
    /// Claim supported Garmin health work for the installed parser using the durable lease.
    /// Offline work does not consume a Garmin request or depend on its cooldown.
    pub async fn claim_health_replay_job(
        &self,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<ReplayJobLease>, JobStoreError> {
        let endpoints: Vec<_> = health_endpoints().collect();
        self.claim_supported_replay(now, lease_seconds, endpoints)
            .await
    }

    /// Claim one offline job handled by the installed Garmin replay worker.
    pub async fn claim_replay_job(
        &self,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<ReplayJobLease>, JobStoreError> {
        let endpoints = DAY_ENDPOINTS
            .iter()
            .copied()
            .chain(["devices"])
            .chain(
                ActivityDocument::ALL
                    .into_iter()
                    .map(ActivityDocument::endpoint),
            )
            .chain(
                ActivitySummaryKind::ALL
                    .into_iter()
                    .map(ActivitySummaryKind::endpoint),
            )
            .chain([FIT_ENDPOINT])
            .collect();
        self.claim_supported_replay(now, lease_seconds, endpoints)
            .await
    }

    async fn claim_supported_replay(
        &self,
        now: StoredInstant,
        lease_seconds: u32,
        endpoints: Vec<&str>,
    ) -> Result<Option<ReplayJobLease>, JobStoreError> {
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
        let token = Uuid::new_v4();
        let claimed =
            sqlx::query_as::<_, (Uuid, i32, String, String)>(include_str!("replay/claim.sql"))
                .bind(GARMIN_PARSER_VERSION)
                .bind(ATTEMPT_LIMIT)
                .bind(OffsetDateTime::from(now))
                .bind(token)
                .bind(seconds)
                .bind(endpoints)
                .fetch_optional(&mut *txn)
                .await?;
        txn.commit().await?;
        Ok(
            claimed.map(|(id, attempt, payload, endpoint)| ReplayJobLease {
                id,
                token,
                seconds,
                attempt,
                payload,
                endpoint,
            }),
        )
    }

    pub async fn renew_replay_job(
        &self,
        lease: &ReplayJobLease,
        now: StoredInstant,
    ) -> Result<bool, JobStoreError> {
        self.renew_job(lease.id, lease.token, lease.seconds, KIND, now, None)
            .await
    }

    /// Release a failed attempt, or refund it when another writer held the replay fence.
    pub async fn fail_replay_job(
        &self,
        lease: &ReplayJobLease,
        busy_until: Option<StoredInstant>,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        let mut txn = begin(&self.work).await?;
        let result = lease
            .finish_in(
                &mut txn,
                JobOutcome::Failed {
                    reason: if busy_until.is_some() {
                        FailureReason::ReplayBusy
                    } else {
                        FailureReason::RuntimeError
                    },
                    retry_at: busy_until,
                },
            )
            .await?;
        txn.commit().await?;
        Ok(result)
    }
}

fn health_endpoints() -> impl Iterator<Item = &'static str> {
    HEALTH_CALENDAR_ENDPOINTS
        .iter()
        .chain(HEALTH_INTRADAY_ENDPOINTS)
        .copied()
}
