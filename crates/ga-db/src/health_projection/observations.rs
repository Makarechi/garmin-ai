use std::collections::BTreeMap;

use ga_domain::{garmin_health::KnownObservation, timestamp::StoredInstant};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};

use crate::{
    activity_documents::{ApplyActivity, DocumentError},
    health_analysis::PRE_EVENT_FEATURE_VERSION,
    system_metrics::SystemMetricVersion,
};

pub(super) async fn publish_observations(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    day: Date,
    observations: &[KnownObservation],
    versions: &BTreeMap<&str, SystemMetricVersion>,
) -> Result<bool, DocumentError> {
    if observations.is_empty() {
        return Ok(false);
    }
    let history = observation_history(txn, request).await?;
    let mut changed = false;
    for observation in observations {
        let version = versions
            .get(observation.metric)
            .ok_or(DocumentError::InvalidRecordedState)?;
        let ordinal =
            i32::try_from(observation.sequence).map_err(|_| DocumentError::InvalidHealthPayload)?;
        let observed = observation.observed.map(OffsetDateTime::from);
        let bounds = observation
            .interval
            .map(|(start, end)| (OffsetDateTime::from(start), OffsetDateTime::from(end)));
        let quality = if observed.is_some() {
            "observed"
        } else {
            "time_unknown"
        };
        for application in &history {
            let ingestion = application
                .clocks
                .get(&(
                    observation.metric.to_owned(),
                    ordinal,
                    PRE_EVENT_FEATURE_VERSION.to_owned(),
                ))
                .copied()
                .or_else(|| application.clocks.values().copied().min())
                .map(OffsetDateTime::from);
            let altered: bool = sqlx::query_scalar(
                "WITH published AS (
                    INSERT INTO metric_observations (id, metric, metric_definition_version_id,
                        value, unit, source_calendar_date, source_ref, fetched_at, ingested_at,
                        timezone, sequence, observed_at, effective_start, effective_end,
                        account, quality, valid, feature_version)
                    VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7,
                        coalesce($8, clock_timestamp()), $9, $10, $11, $12, $13, $14, $15, true, $16)
                    ON CONFLICT DO NOTHING RETURNING id
                 ), previous AS (
                    SELECT value, unit, timezone, effective_start, effective_end, account, quality
                    FROM metric_observations WHERE metric = $1 AND source_calendar_date = $5
                       AND sequence = $10 AND observed_at IS NOT DISTINCT FROM $11
                       AND feature_version = $16
                    ORDER BY fetched_at DESC, ingested_at DESC, sequence DESC LIMIT 1
                 )
                 SELECT EXISTS (SELECT 1 FROM published) AND NOT EXISTS (
                    SELECT 1 FROM previous WHERE (value, unit, timezone, effective_start, effective_end,
                       account, quality) IS NOT DISTINCT FROM ($3, $4, $9, $12, $13, $14, $15)
                 )",
            )
            .bind(observation.metric).bind(version.id).bind(observation.value).bind(observation.unit)
            .bind(day).bind(request.source_id).bind(OffsetDateTime::from(application.fetched))
            .bind(ingestion).bind(&application.zone).bind(ordinal).bind(observed)
            .bind(bounds.map(|(start,_)| start)).bind(bounds.map(|(_,end)| end))
            .bind(request.account_fingerprint).bind(quality).bind(PRE_EVENT_FEATURE_VERSION)
            .fetch_one(&mut **txn).await?;
            changed |= altered;
        }
    }
    Ok(changed)
}

struct Application {
    fetched: StoredInstant,
    zone: String,
    clocks: BTreeMap<(String, i32, String), StoredInstant>,
}

async fn observation_history(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
) -> Result<Vec<Application>, DocumentError> {
    let key = format!("observation-owner:{}", request.source_id);
    let saved: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
            .bind(key)
            .fetch_optional(&mut **txn)
            .await?;
    let mut history = vec![Application {
        fetched: request.requested_at,
        zone: request.timezone.to_owned(),
        clocks: BTreeMap::new(),
    }];
    let Some(saved) = saved else {
        return Ok(history);
    };
    let saved: Value =
        serde_json::from_str(&saved).map_err(|_| DocumentError::InvalidRecordedState)?;
    let Some(times) = saved["times"].as_object() else {
        return Ok(history);
    };
    for (key, clock) in times {
        let mut parts = key.rsplitn(4, '|');
        let representation = parts.next().ok_or(DocumentError::InvalidRecordedState)?;
        let sequence = parts
            .next()
            .and_then(|text| text.parse::<i32>().ok())
            .ok_or(DocumentError::InvalidRecordedState)?;
        let metric = parts.next().ok_or(DocumentError::InvalidRecordedState)?;
        let fetched = parts
            .next()
            .and_then(|text| text.parse::<StoredInstant>().ok())
            .ok_or(DocumentError::InvalidRecordedState)?;
        let clock = clock
            .as_str()
            .and_then(|text| text.parse::<StoredInstant>().ok())
            .ok_or(DocumentError::InvalidRecordedState)?;
        let index = history
            .iter()
            .position(|application| application.fetched == fetched)
            .unwrap_or_else(|| {
                let zone = saved["zones"][key].as_str().unwrap_or(request.timezone);
                history.push(Application {
                    fetched,
                    zone: zone.to_owned(),
                    clocks: BTreeMap::new(),
                });
                history.len() - 1
            });
        history[index].clocks.insert(
            (metric.to_owned(), sequence, representation.to_owned()),
            clock,
        );
    }
    Ok(history)
}
