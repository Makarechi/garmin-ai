use std::collections::BTreeMap;

use futures_util::TryStreamExt;
use ga_domain::garmin_health::{
    HealthReplacement, Measurement, normalize_health_payload, replacement_channels,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use crate::{
    activity_documents::{ApplyActivity, DocumentError, put_state},
    raw_capture::GARMIN_LEGACY_SOURCE,
    system_metrics::SystemMetricVersion,
};

const BATCH_ROWS: usize = 512;
const MAX_HISTORY: usize = 1_000;

pub(super) async fn publish_measurements(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    samples: &[Measurement],
    versions: &BTreeMap<&str, SystemMetricVersion>,
    attestation: Option<&HealthReplacement>,
) -> Result<bool, DocumentError> {
    let mut changed = false;
    for chunk in samples.chunks(BATCH_ROWS) {
        let incoming: Vec<_> = chunk.iter().enumerate().map(|(ordinal, sample)| {
            let instant = sample.instant.to_string();
            let version = versions
                .get(sample.metric)
                .ok_or(DocumentError::InvalidRecordedState)?;
            Ok(json!({
                "ordinal": ordinal, "ts": instant, "metric": sample.metric,
                "value": sample.value, "unit": sample.unit, "version": version.id.to_string(),
                "ownership": format!("sample-owner:{instant}:{}:{GARMIN_LEGACY_SOURCE}", sample.metric),
            }))
        }).collect::<Result<_, DocumentError>>()?;
        let altered: bool = sqlx::query_scalar(
            "WITH received AS MATERIALIZED (
                SELECT incoming.*, (incoming.ts AT TIME ZONE $2)::date AS calendar_day
                FROM jsonb_to_recordset($1::jsonb) AS incoming(
                    ordinal integer, ts timestamptz, metric text, value float8,
                    unit text, version uuid, ownership text)
             ), first_observations AS (
                SELECT DISTINCT ON (ts, metric) * FROM received
                ORDER BY ts, metric, ordinal ASC
             ), current_candidates AS (
                SELECT DISTINCT ON (ts, metric) * FROM received
                ORDER BY ts, metric, ordinal DESC
             ), publishable AS MATERIALIZED (
                SELECT candidate.* FROM current_candidates AS candidate
                LEFT JOIN measurements AS current ON current.ts = candidate.ts
                    AND current.metric = candidate.metric AND current.source = $3
                WHERE current.ingested_at IS NULL OR current.ingested_at <= $5
             ), removed_owner AS (
                DELETE FROM app_state USING publishable
                WHERE app_state.key = publishable.ownership
             ), recorded AS (
                INSERT INTO measurement_revisions (id, ts, metric, source, local_date, value, unit,
                    metric_definition_version_id, source_ref, quality, details, ingested_at, deleted)
                SELECT gen_random_uuid(), ts, metric, $3, calendar_day, value, unit,
                    version, $4, 'observed', '{}'::jsonb, $5, false FROM first_observations
                ON CONFLICT (ts, metric, source, source_ref, ingested_at, deleted) DO NOTHING
             ), published AS (
                INSERT INTO measurements (ts, metric, source, local_date, value, unit,
                    metric_definition_version_id, source_ref, quality, details, ingested_at)
                SELECT ts, metric, $3, calendar_day, value, unit, version, $4, 'observed', '{}'::jsonb, $5
                FROM publishable
                ON CONFLICT (ts, metric, source) DO UPDATE
                SET local_date = excluded.local_date, value = excluded.value, unit = excluded.unit,
                    metric_definition_version_id = excluded.metric_definition_version_id,
                    source_ref = excluded.source_ref, ingested_at = excluded.ingested_at
                WHERE measurements.ingested_at <= excluded.ingested_at
                  AND (measurements.local_date, measurements.value, measurements.unit,
                    measurements.metric_definition_version_id, measurements.source_ref, measurements.ingested_at)
                  IS DISTINCT FROM (excluded.local_date, excluded.value, excluded.unit,
                    excluded.metric_definition_version_id, excluded.source_ref, excluded.ingested_at)
                RETURNING ts, metric
             )
             SELECT EXISTS (
                SELECT 1 FROM received AS candidate
                LEFT JOIN measurements AS previous ON previous.ts = candidate.ts
                    AND previous.metric = candidate.metric AND previous.source = $3
                WHERE (previous.ingested_at IS NULL OR previous.ingested_at <= $5)
                  AND ($6::timestamptz IS NULL OR candidate.ts < $6 OR candidate.ts >= $7
                       OR NOT (candidate.metric=ANY($8::text[])))
                  AND (previous.local_date, previous.value, previous.unit, previous.metric_definition_version_id)
                  IS DISTINCT FROM (candidate.calendar_day, candidate.value, candidate.unit, candidate.version)
             )",
        )
        .bind(Value::Array(incoming).to_string())
        .bind(request.timezone)
        .bind(GARMIN_LEGACY_SOURCE)
        .bind(request.source_id)
        .bind(OffsetDateTime::from(request.requested_at))
        .bind(attestation.map(|proof| OffsetDateTime::from(proof.start())))
        .bind(attestation.map(|proof| OffsetDateTime::from(proof.end())))
        .bind(attestation.map(HealthReplacement::metrics))
        .fetch_one(&mut **txn)
        .await?;
        changed |= altered;
    }
    Ok(changed)
}

pub(super) async fn record_application(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    endpoint: &str,
    document: &Value,
    attestation: Option<&HealthReplacement>,
) -> Result<(), DocumentError> {
    if replacement_channels(endpoint).is_empty()
        || (attestation.is_none()
            && (empty_field(document)
                || document
                    .as_object()
                    .is_some_and(|object| object.values().all(empty_field))))
    {
        return Ok(());
    }
    let key = format!(
        "ingest-history:{GARMIN_LEGACY_SOURCE}:{endpoint}:{}",
        request.source_key
    );
    let saved: Option<String> =
        sqlx::query_scalar("SELECT (value->'applications')::text FROM app_state WHERE key = $1")
            .bind(&key)
            .fetch_optional(&mut **txn)
            .await?;
    let mut applications: Vec<Value> = match saved {
        Some(saved) => {
            serde_json::from_str(&saved).map_err(|_| DocumentError::InvalidRecordedState)?
        }
        None => {
            if previous_sample_evidence(txn, request, endpoint).await? {
                vec![json!({"legacy_order_unknown": true})]
            } else {
                Vec::new()
            }
        }
    };
    if applications.len() >= MAX_HISTORY {
        return Err(DocumentError::InvalidRecordedState);
    }
    applications.push(json!({
        "raw_ref": request.source_id.to_string(), "at": request.requested_at.to_string(),
        "timezone": request.timezone, "replacement": attestation.map(HealthReplacement::document),
    }));
    put_state(txn, &key, &json!({"applications": applications})).await?;
    Ok(())
}

async fn previous_sample_evidence(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    endpoint: &str,
) -> Result<bool, DocumentError> {
    let mut earlier = sqlx::query_as::<_, (Option<String>, bool)>(
        "SELECT raw.payload::text, EXISTS (SELECT 1 FROM measurements WHERE source_ref = raw.id)
         FROM source_payloads AS raw
         WHERE raw.source = $1 AND raw.endpoint = $2 AND raw.source_key = $3 AND raw.id != $4
           AND (raw.status IN ('normalized','partial') OR EXISTS (
                   SELECT 1 FROM measurements WHERE source_ref = raw.id
                ))",
    )
    .bind(GARMIN_LEGACY_SOURCE)
    .bind(endpoint)
    .bind(request.source_key)
    .bind(request.source_id)
    .fetch(&mut **txn);
    while let Some((document, owns_samples)) = earlier.try_next().await? {
        if owns_samples {
            return Ok(true);
        }
        if let Some(document) = document {
            let document: Value =
                serde_json::from_str(&document).map_err(|_| DocumentError::InvalidRecordedState)?;
            if !matches!(normalize_health_payload(endpoint, request.source_key, &document),
                Ok(projection) if projection.samples.is_empty())
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

fn empty_field(value: &Value) -> bool {
    value.is_null()
        || value.as_object().is_some_and(serde_json::Map::is_empty)
        || value.as_array().is_some_and(Vec::is_empty)
}
