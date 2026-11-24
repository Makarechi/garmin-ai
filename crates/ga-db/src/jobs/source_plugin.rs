use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use time::OffsetDateTime;
use uuid::Uuid;

use super::{ATTEMPT_LIMIT, JobOutcome, JobStoreError, RecordedJobStatus, begin, fail_exhausted};
use crate::{
    Database, scenario_packs::source_instance_selected, share_policy::OWNER_ONBOARDING_LOCK,
};

const KIND: &str = "source_plugin_poll";
const LEASE_SECONDS: i32 = 120;

pub struct SourcePluginLease {
    id: Uuid,
    token: Uuid,
    instance: String,
}

impl SourcePluginLease {
    #[must_use]
    pub fn instance(&self) -> &str {
        &self.instance
    }
}

impl Database {
    /// Preserve the existing per-instance UTC hour and job schema during upgrades.
    pub async fn schedule_source_plugin_polls(
        &self,
        instances: &[&str],
        now: StoredInstant,
    ) -> Result<(), JobStoreError> {
        let mut txn = begin(&self.work).await?;
        let admitted: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
            .bind(OWNER_ONBOARDING_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !admitted {
            return Err(JobStoreError::Busy);
        }
        let at = OffsetDateTime::from(now);
        let hour = at.unix_timestamp().div_euclid(3600);
        for instance in instances {
            if source_instance_selected(&mut txn, instance).await? {
                sqlx::query(
                    "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
                     VALUES(gen_random_uuid(),$1,$2::jsonb,$3,'pending',$4,0)
                     ON CONFLICT(dedup_key) DO NOTHING",
                )
                .bind(KIND)
                .bind(json!({"instance_id":instance}).to_string())
                .bind(format!("source-plugin:{instance}:{hour}"))
                .bind(at)
                .execute(&mut *txn)
                .await?;
            }
        }
        txn.commit().await?;
        Ok(())
    }

    /// Resume only jobs for explicitly configured executable instances.
    pub async fn claim_source_plugin_poll(
        &self,
        instances: &[&str],
        now: StoredInstant,
    ) -> Result<Option<SourcePluginLease>, JobStoreError> {
        let mut txn = begin(&self.work).await?;
        let at = OffsetDateTime::from(now);
        fail_exhausted(&mut txn, at, Some(&[KIND])).await?;
        let token = Uuid::new_v4();
        let claimed: Option<(Uuid, String)> = sqlx::query_as(
            "WITH eligible AS (
                SELECT id FROM jobs
                WHERE kind=$1 AND payload->>'instance_id'=ANY($2::text[])
                  AND attempts<$3 AND
                    ((status='pending' AND run_at<=$4) OR (status='running' AND lease_until<$4))
                ORDER BY run_at,id LIMIT 1 FOR UPDATE SKIP LOCKED
             )
             UPDATE jobs SET status='running',attempts=attempts+1,lease_token=$5,
                lease_until=$4::timestamptz+$6*interval '1 second'
             FROM eligible WHERE jobs.id=eligible.id
             RETURNING jobs.id,jobs.payload->>'instance_id'",
        )
        .bind(KIND)
        .bind(instances)
        .bind(ATTEMPT_LIMIT)
        .bind(at)
        .bind(token)
        .bind(LEASE_SECONDS)
        .fetch_optional(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(claimed.map(|(id, instance)| SourcePluginLease {
            id,
            token,
            instance,
        }))
    }

    pub async fn finish_source_plugin_poll(
        &self,
        lease: &SourcePluginLease,
        now: StoredInstant,
        outcome: JobOutcome,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        self.finish_job(lease.id, lease.token, KIND, now, outcome)
            .await
    }
}
