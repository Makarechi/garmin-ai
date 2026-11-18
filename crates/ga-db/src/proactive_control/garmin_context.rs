use std::time::Duration;

use futures_util::TryStreamExt;
use ga_domain::timestamp::StoredInstant;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use super::ProactiveControlError;
use crate::{
    Database, activity_documents::PARSER_VERSION, garmin_integration::garmin_pause,
    replay::projection_pending,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiaryContextAvailability {
    WaitingForSync,
    Withheld,
    Ready,
}

impl Database {
    /// Check the Garmin dependencies used by the diary notification worker.
    pub async fn diary_context_availability(
        &self,
        now: StoredInstant,
    ) -> Result<DiaryContextAvailability, ProactiveControlError> {
        tokio::time::timeout(Duration::from_secs(5), async {
            let mut txn = self.begin_diary_question_policy().await?;
            let result = availability_in(&mut txn, now).await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| ProactiveControlError::Database)?
    }
}

pub(super) async fn availability_in(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
) -> Result<DiaryContextAvailability, ProactiveControlError> {
    use DiaryContextAvailability::{Ready, WaitingForSync, Withheld};

    if garmin_pause(&mut **txn, now)
        .await
        .map_err(|_| ProactiveControlError::InvalidStoredState)?
        .is_some()
    {
        return Ok(Withheld);
    }
    let pending: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM jobs
                       WHERE payload->'backfill' IS DISTINCT FROM 'true'::jsonb
                         AND (kind='garmin_activities' OR (kind='garmin_endpoint'
                              AND payload->>'endpoint' IN ('heart_rate','stress')))
                         AND status IN ('pending','running'))",
    )
    .fetch_one(&mut **txn)
    .await?;
    if pending {
        return Ok(WaitingForSync);
    }
    if projection_pending(txn, PARSER_VERSION).await? {
        return Ok(Withheld);
    }
    let mut failed = sqlx::query_as::<_, (OffsetDateTime, Option<String>)>(
        "SELECT coalesce(job.completed_at,job.run_at) AS finished,
                CASE WHEN fresh.value ? 'normalized_at' THEN fresh.value->>'normalized_at'
                     WHEN coalesce(fresh.value->>'status','') NOT IN ('error','fetch_error')
                     THEN fresh.value->>'success_at' END
         FROM jobs job
         LEFT JOIN app_state fresh ON fresh.key = CASE
           WHEN job.kind='garmin_activities' THEN
                concat('freshness:activities:page:',coalesce(job.payload->>'offset','0'))
           ELSE concat('freshness:',job.payload->>'endpoint',':',job.payload->>'key') END
         WHERE job.status='failed' AND job.payload->'backfill' IS DISTINCT FROM 'true'::jsonb
           AND (job.kind='garmin_activities' OR (job.kind='garmin_endpoint'
                AND job.payload->>'endpoint' IN ('heart_rate','stress')))
           AND coalesce(job.completed_at,job.run_at)>=$1::timestamptz-interval '3 hours'",
    )
    .bind(OffsetDateTime::from(now))
    .fetch(&mut **txn);
    while let Some((finished, succeeded)) = failed.try_next().await? {
        if succeeded
            .as_deref()
            .and_then(|stamp| stamp.parse::<StoredInstant>().ok())
            .is_none_or(|success| OffsetDateTime::from(success) < finished)
        {
            return Ok(Withheld);
        }
    }
    Ok(Ready)
}
