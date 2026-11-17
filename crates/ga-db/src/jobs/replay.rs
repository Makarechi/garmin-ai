use ga_domain::timestamp::StoredInstant;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ATTEMPT_LIMIT, JobOutcome, JobStoreError, RecordedJobStatus, STORAGE_QUEUE_LOCK, begin,
    fail_exhausted, finish_in_transaction,
};
use crate::{Database, replay::GARMIN_PARSER_VERSION};

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
    /// Claim archived replay work for the installed parser using the existing durable lease.
    /// Offline work does not consume a Garmin request or depend on its cooldown.
    pub async fn claim_replay_job(
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
        fail_exhausted(&mut txn, now.into(), Some(&[KIND])).await?;
        let token = Uuid::new_v4();
        let claimed = sqlx::query_as::<_, (Uuid, i32, String)>(
            "WITH candidate AS (
                 SELECT id FROM jobs
                 WHERE kind='raw_replay' AND payload->>'target_version'=$1::int::text
                   AND attempts<$2
                   AND ((status='pending' AND run_at<=$3)
                      OR (status='running' AND lease_until<$3))
                 ORDER BY (payload->'backfill' IS NOT DISTINCT FROM 'true'::jsonb),run_at,id
                 FOR UPDATE SKIP LOCKED LIMIT 1
             )
             UPDATE jobs SET status='running',attempts=attempts+1,lease_token=$4,
                             lease_until=$3+$5*interval '1 second'
             FROM candidate WHERE jobs.id=candidate.id
             RETURNING jobs.id,jobs.attempts,jobs.payload::text",
        )
        .bind(GARMIN_PARSER_VERSION)
        .bind(ATTEMPT_LIMIT)
        .bind(OffsetDateTime::from(now))
        .bind(token)
        .bind(seconds)
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
