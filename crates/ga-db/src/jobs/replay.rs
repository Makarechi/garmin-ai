use ga_domain::timestamp::StoredInstant;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ATTEMPT_LIMIT, JobOutcome, JobStoreError, RecordedJobStatus, STORAGE_QUEUE_LOCK, begin,
    finish_in_transaction,
};
use crate::{
    Database,
    replay::{GARMIN_PARSER_VERSION, HEALTH_CALENDAR_ENDPOINTS, HEALTH_INTRADAY_ENDPOINTS},
};

const KIND: &str = "raw_replay";

pub struct ReplayJobLease {
    id: Uuid,
    token: Uuid,
    seconds: i32,
    attempt: i32,
    payload: String,
}

impl ReplayJobLease {
    pub fn id(&self) -> Uuid {
        self.id
    }

    pub fn attempt(&self) -> i32 {
        self.attempt
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
        let endpoints: Vec<_> = HEALTH_CALENDAR_ENDPOINTS
            .iter()
            .chain(HEALTH_INTRADAY_ENDPOINTS)
            .copied()
            .collect();
        let claimed =
            sqlx::query_as::<_, (Uuid, i32, String)>(include_str!("replay/claim_health.sql"))
                .bind(GARMIN_PARSER_VERSION)
                .bind(ATTEMPT_LIMIT)
                .bind(OffsetDateTime::from(now))
                .bind(token)
                .bind(seconds)
                .bind(endpoints)
                .fetch_optional(&mut *txn)
                .await?;
        txn.commit().await?;
        Ok(claimed.map(|(id, attempt, payload)| ReplayJobLease {
            id,
            token,
            seconds,
            attempt,
            payload,
        }))
    }

    pub async fn renew_replay_job(
        &self,
        lease: &ReplayJobLease,
        now: StoredInstant,
    ) -> Result<bool, JobStoreError> {
        self.renew_job(lease.id, lease.token, lease.seconds, KIND, now)
            .await
    }
}
