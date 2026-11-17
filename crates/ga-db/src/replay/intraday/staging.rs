use std::collections::BTreeMap;

use ga_domain::{
    garmin_health::{HealthReplacement, Measurement},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    activity_documents::{ApplyActivity, DocumentError},
    raw_capture::GARMIN_LEGACY_SOURCE,
    system_metrics::SystemMetricVersion,
};

const BATCH_ROWS: usize = 512;

pub(super) async fn snapshot(
    txn: &mut Transaction<'_, Postgres>,
    raw: Uuid,
) -> Result<bool, DocumentError> {
    sqlx::query(include_str!("snapshot.sql"))
        .bind(raw)
        .execute(&mut **txn)
        .await?;
    let owned: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM pg_temp.ga_replay_sample_targets)")
            .fetch_one(&mut **txn)
            .await?;
    if owned {
        sqlx::query("CREATE UNIQUE INDEX ON pg_temp.ga_replay_sample_targets(ts,metric,source)")
            .execute(&mut **txn)
            .await?;
    }
    Ok(owned)
}

pub(super) async fn unknown_order(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), DocumentError> {
    sqlx::query("UPDATE pg_temp.ga_replay_sample_targets SET unknown_order=true, restored_at=NULL")
        .execute(&mut **txn)
        .await?;
    Ok(())
}

pub(super) async fn forget_covered(
    txn: &mut Transaction<'_, Postgres>,
    replacement: &HealthReplacement,
) -> Result<(), DocumentError> {
    sqlx::query(
        "UPDATE pg_temp.ga_replay_sample_targets SET unknown_order=false, restored_at=NULL
         WHERE metric=ANY($1::text[]) AND ts >= $2 AND ts < $3",
    )
    .bind(replacement.metrics())
    .bind(OffsetDateTime::from(replacement.start()))
    .bind(OffsetDateTime::from(replacement.end()))
    .execute(&mut **txn)
    .await?;
    Ok(())
}

pub(super) async fn record(
    txn: &mut Transaction<'_, Postgres>,
    measurements: &[Measurement],
    at: StoredInstant,
    timezone: &str,
) -> Result<(), DocumentError> {
    for chunk in measurements.chunks(BATCH_ROWS) {
        let records: Vec<_> = chunk
            .iter()
            .enumerate()
            .map(|(index, sample)| {
                json!({
                    "ts": sample.instant.to_string(), "metric": sample.metric,
                    "value": sample.value, "unit": sample.unit, "offset_in_body": index,
                })
            })
            .collect();
        sqlx::query(include_str!("stage_application.sql"))
            .bind(Value::Array(records).to_string())
            .bind(timezone)
            .bind(OffsetDateTime::from(at))
            .bind(GARMIN_LEGACY_SOURCE)
            .execute(&mut **txn)
            .await?;
    }
    Ok(())
}

pub(super) async fn replace_owned(
    txn: &mut Transaction<'_, Postgres>,
    source: Uuid,
    parser_changed: bool,
    versions: &BTreeMap<&str, SystemMetricVersion>,
) -> Result<(), DocumentError> {
    if parser_changed {
        sqlx::query(include_str!("retain_owners.sql"))
            .execute(&mut **txn)
            .await?;
    }
    sqlx::query("DELETE FROM measurements WHERE source_ref=$1")
        .bind(source)
        .execute(&mut **txn)
        .await?;
    let versions: Vec<_> = versions
        .iter()
        .map(|(key, version)| json!({"metric":key,"id":version.id.to_string()}))
        .collect();
    sqlx::query(include_str!("restore.sql"))
        .bind(source)
        .bind(Value::Array(versions).to_string())
        .execute(&mut **txn)
        .await?;
    Ok(())
}

pub(super) async fn retain_replay_result(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
) -> Result<(), DocumentError> {
    let unresolved: bool = sqlx::query_scalar(
        "SELECT EXISTS (
            SELECT 1 FROM pg_temp.ga_replay_sample_targets AS target
            WHERE unknown_order AND NOT EXISTS (
                SELECT 1 FROM measurements AS current
                WHERE (current.ts,current.metric,current.source)=(target.ts,target.metric,target.source)
                  AND current.source_ref=$1
            )
        )",
    )
    .bind(request.source_id)
    .fetch_one(&mut **txn)
    .await?;
    if unresolved {
        return Err(DocumentError::InvalidRecordedState);
    }
    sqlx::query(include_str!("publish_revision.sql"))
        .bind(OffsetDateTime::from(request.requested_at))
        .execute(&mut **txn)
        .await?;
    Ok(())
}

pub(super) async fn admit(
    txn: &mut Transaction<'_, Postgres>,
    samples: Vec<Measurement>,
    raw: Uuid,
    order: &Value,
    later_replacements: &Value,
) -> Result<Vec<Measurement>, DocumentError> {
    let raw = raw.to_string();
    let order = order.to_string();
    let later_replacements = later_replacements.to_string();
    let mut accepted = Vec::new();
    for chunk in samples.chunks(BATCH_ROWS) {
        let candidates = chunk.iter().enumerate().map(|(index,sample)| {
            json!({
                "ordinal": index, "ts": sample.instant.to_string(), "metric": sample.metric,
                "owner_key": format!("sample-owner:{}:{}:{GARMIN_LEGACY_SOURCE}",sample.instant,sample.metric),
            })
        }).collect();
        let indexes: Vec<i32> = sqlx::query_scalar(include_str!("admit_samples.sql"))
            .bind(Value::Array(candidates).to_string())
            .bind(&raw)
            .bind(&order)
            .bind(&later_replacements)
            .bind(GARMIN_LEGACY_SOURCE)
            .fetch_all(&mut **txn)
            .await?;
        for index in indexes {
            let sample = usize::try_from(index)
                .ok()
                .and_then(|index| chunk.get(index))
                .ok_or(DocumentError::InvalidRecordedState)?;
            accepted.push(*sample);
        }
    }
    Ok(accepted)
}
