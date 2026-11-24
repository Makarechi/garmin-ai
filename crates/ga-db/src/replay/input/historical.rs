use std::collections::BTreeSet;

use futures_util::TryStreamExt;
use ga_domain::{
    event_input::known_timezone, garmin_health::normalize_health_payload, timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{ReplayInputError, ReplayReplacement, ReplaySource};

const MAX_APPLICATIONS: usize = 1000;
const MAX_INTERVAL_MICROSECONDS: i64 = 31 * 24 * 60 * 60 * 1_000_000;

pub(crate) async fn fit_clock(
    txn: &mut Transaction<'_, Postgres>,
    raw: &ReplaySource,
) -> Result<StoredInstant, ReplayInputError> {
    let state = app_state(txn, &format!("fit-version:{}", raw.source_key)).await?;
    let reference = raw.id.to_string();
    let clock = if owns(&state["latest_attempt"], &reference) {
        &state["latest_attempt"]["requested_at"]
    } else if owns(&state, &reference) {
        &state["requested_at"]
    } else {
        &Value::Null
    };
    Ok(optional_clock(clock)?.unwrap_or(raw.fetched_at))
}

pub(super) async fn json_context(
    txn: &mut Transaction<'_, Postgres>,
    raw: &ReplaySource,
    payload: &Value,
    configured_timezone: &str,
    max_bytes: u64,
) -> Result<(StoredInstant, String, Option<ReplayReplacement>), ReplayInputError> {
    let scope = format!("garmin_connect:{}:{}", raw.endpoint, raw.source_key);
    let state = app_state(txn, &format!("ingest:{scope}")).await?;
    let metadata = app_state(txn, &format!("ingest-meta:{}", raw.id)).await?;
    let reference = raw.id.to_string();
    let attempt = &state["latest_attempt"];
    let latest_attempt = owns(attempt, &reference);
    let current = owns(&state, &reference);

    let mut at = raw.fetched_at;
    if latest_attempt {
        at = parse_clock(&attempt["requested_at"])?;
    } else if current {
        at = optional_clock(&state["requested_at"])?.unwrap_or(at);
    }
    let applied = optional_clock(&metadata["applied_at"])?;
    let latest_observation = latest_observation(txn, raw.id).await?;
    match applied {
        Some(applied) if !latest_attempt => at = at.max(applied),
        None => {
            at = retained_day_clock(txn, &reference, at).await?;
            if let Some(observed) = latest_observation {
                at = at.max(
                    observed
                        .try_into()
                        .map_err(|_| ReplayInputError::HistoricalClock)?,
                );
            }
        }
        Some(_) => {}
    }

    let timezone = historical_timezone(
        txn,
        raw,
        payload,
        &metadata,
        latest_observation,
        configured_timezone,
    )
    .await?;
    let replacement = if latest_attempt {
        read_replacement(&attempt["replacement"], &raw.endpoint)?
    } else if current {
        read_replacement(&state["replacement"], &raw.endpoint)?
    } else if !channels(&raw.endpoint).is_empty() {
        let record = app_state(txn, &format!("ingest-history:{scope}")).await?;
        if record.is_null() {
            if legacy_application_order_unknown(txn, raw, max_bytes).await? {
                return Err(ReplayInputError::HistoricalContract);
            }
            None
        } else {
            let applications = record["applications"]
                .as_array()
                .filter(|values| values.len() <= MAX_APPLICATIONS)
                .ok_or(ReplayInputError::HistoricalContract)?;
            if let Some(saved) = applications
                .iter()
                .rev()
                .find(|entry| entry["raw_ref"].as_str() == Some(&reference))
            {
                at = parse_clock(&saved["at"])?;
                read_replacement(&saved["replacement"], &raw.endpoint)?
            } else if applications.is_empty() {
                None
            } else {
                return Err(ReplayInputError::HistoricalContract);
            }
        }
    } else {
        None
    };
    Ok((at, timezone, replacement))
}

async fn app_state(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
) -> Result<Value, ReplayInputError> {
    let stored: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
            .bind(key)
            .fetch_optional(&mut **txn)
            .await?;
    stored
        .map(|text| serde_json::from_str(&text).map_err(|_| ReplayInputError::HistoricalContract))
        .transpose()
        .map(|value| value.unwrap_or(Value::Null))
}

fn owns(state: &Value, reference: &str) -> bool {
    state["source_ref"].as_str() == Some(reference)
}

fn parse_clock(value: &Value) -> Result<StoredInstant, ReplayInputError> {
    value
        .as_str()
        .and_then(|value| value.parse().ok())
        .ok_or(ReplayInputError::HistoricalClock)
}

fn optional_clock(value: &Value) -> Result<Option<StoredInstant>, ReplayInputError> {
    if value.is_null() || value.as_str() == Some("") {
        Ok(None)
    } else {
        parse_clock(value).map(Some)
    }
}

async fn retained_day_clock(
    txn: &mut Transaction<'_, Postgres>,
    reference: &str,
    mut at: StoredInstant,
) -> Result<StoredInstant, ReplayInputError> {
    let mut clocks = sqlx::query_scalar::<_, Option<String>>(
        "SELECT day.sources->>concat('time:', substr(field.key, 7))
         FROM health_days AS day CROSS JOIN LATERAL jsonb_each_text(
             CASE WHEN jsonb_typeof(day.sources)='object' THEN day.sources ELSE '{}'::jsonb END
         ) AS field(key,value)
         WHERE starts_with(field.key,'field:') AND field.value=$1",
    )
    .bind(reference)
    .fetch(&mut **txn);
    while let Some(clock) = clocks.try_next().await? {
        if let Some(value) = clock.filter(|value| !value.is_empty()) {
            at = at.max(
                value
                    .parse()
                    .map_err(|_| ReplayInputError::HistoricalClock)?,
            );
        }
    }
    Ok(at)
}

async fn latest_observation(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
) -> Result<Option<OffsetDateTime>, ReplayInputError> {
    Ok(
        sqlx::query_scalar("SELECT max(fetched_at) FROM metric_observations WHERE source_ref=$1")
            .bind(id)
            .fetch_one(&mut **txn)
            .await?,
    )
}

async fn historical_timezone(
    txn: &mut Transaction<'_, Postgres>,
    raw: &ReplaySource,
    payload: &Value,
    metadata: &Value,
    latest_observation: Option<OffsetDateTime>,
    configured: &str,
) -> Result<String, ReplayInputError> {
    let existing = if !metadata["timezone"].is_null() {
        Some(
            metadata["timezone"]
                .as_str()
                .ok_or(ReplayInputError::HistoricalTimezone)?
                .to_owned(),
        )
    } else if let Some(latest) = latest_observation {
        let mut zones: Vec<Option<String>> = sqlx::query_scalar(
            "SELECT DISTINCT timezone FROM metric_observations WHERE source_ref=$1 AND fetched_at=$2 LIMIT 2",
        )
        .bind(raw.id)
        .bind(latest)
        .fetch_all(&mut **txn)
        .await?;
        if zones.len() == 1 {
            zones.pop().flatten()
        } else {
            None
        }
    } else {
        None
    };
    let zone = if let Some(existing) = existing {
        existing
    } else {
        fallback_timezone(txn, raw, payload, configured).await?
    };
    if !known_timezone(&zone) {
        return Err(ReplayInputError::HistoricalTimezone);
    }
    Ok(zone)
}

async fn fallback_timezone(
    txn: &mut Transaction<'_, Postgres>,
    raw: &ReplaySource,
    payload: &Value,
    configured: &str,
) -> Result<String, ReplayInputError> {
    let endpoint = raw.endpoint.as_str();
    match endpoint {
        "daily" | "body_battery" | "hydration" | "max_metrics" | "sleep" | "readiness" => {
            return Ok(configured.to_owned());
        }
        "activity" => {
            if let Some(timezone) =
                sqlx::query_scalar("SELECT timezone FROM activities WHERE id=$1")
                    .bind(&raw.source_key)
                    .fetch_optional(&mut **txn)
                    .await?
            {
                return Ok(timezone);
            }
        }
        "activities" => {
            verify_activity_list_zones(txn, raw, payload).await?;
            return Ok(configured.to_owned());
        }
        endpoint if !channels(endpoint).is_empty() => {
            let has_measurements: bool = sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM measurements WHERE source_ref=$1)",
            )
            .bind(raw.id)
            .fetch_one(&mut **txn)
            .await?;
            if !has_measurements && !has_timed_points(endpoint, payload)? {
                return Ok(configured.to_owned());
            }
        }
        _ => {}
    }
    if matches!(raw.status.as_str(), "normalized" | "partial") {
        Err(ReplayInputError::HistoricalTimezone)
    } else {
        Ok(configured.to_owned())
    }
}

async fn verify_activity_list_zones(
    txn: &mut Transaction<'_, Postgres>,
    raw: &ReplaySource,
    payload: &Value,
) -> Result<(), ReplayInputError> {
    let items = payload
        .as_array()
        .ok_or(ReplayInputError::HistoricalTimezone)?;
    if raw.previous_parser_version <= 0 {
        return Ok(());
    }
    let missing: BTreeSet<String> = items
        .iter()
        .filter(|item| !truthy(&item["timeZoneUnitDTO"]["timeZone"]))
        .map(|item| {
            let id = &item["activityId"];
            if let Some(text) = id.as_str() {
                text.to_owned()
            } else if id.is_null() {
                "None".to_owned()
            } else {
                id.to_string()
            }
        })
        .collect();
    if missing.is_empty() {
        return Ok(());
    }
    let ids: Vec<_> = missing.into_iter().collect();
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM activities WHERE id=ANY($1)")
        .bind(&ids)
        .fetch_one(&mut **txn)
        .await?;
    if usize::try_from(count).ok() == Some(ids.len()) {
        Ok(())
    } else {
        Err(ReplayInputError::HistoricalTimezone)
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::String(value) => !value.is_empty(),
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn has_timed_points(endpoint: &str, payload: &Value) -> Result<bool, ReplayInputError> {
    if endpoint == "steps" {
        let buckets = payload.as_array().ok_or(ReplayInputError::InvalidPayload)?;
        return Ok(buckets.iter().any(|point| truthy(&point["startGMT"])));
    }
    let payload = payload
        .as_object()
        .ok_or(ReplayInputError::InvalidPayload)?;
    let arrays: &[&str] = match endpoint {
        "hrv" => &["hrvReadings"],
        "heart_rate" => &["heartRateValues"],
        "stress" => &["stressValuesArray", "bodyBatteryValuesArray"],
        "respiration" => &["respirationValuesArray"],
        "spo2" => &["spO2HourlyAverages", "spO2ValuesArray"],
        _ => &[],
    };
    for key in arrays {
        let Some(points) = payload.get(*key).filter(|points| truthy(points)) else {
            continue;
        };
        if points
            .as_array()
            .ok_or(ReplayInputError::InvalidPayload)?
            .iter()
            .any(|point| {
                if let Some(values) = point.as_array() {
                    values.first().is_some_and(|timestamp| !timestamp.is_null())
                } else {
                    truthy(&point["readingTimeGMT"])
                }
            })
        {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(in crate::replay) async fn legacy_application_order_unknown(
    txn: &mut Transaction<'_, Postgres>,
    raw: &ReplaySource,
    max_bytes: u64,
) -> Result<bool, ReplayInputError> {
    let mut older = sqlx::query_as::<_, (bool, Option<String>)>(
        "SELECT owned.exists,
                CASE WHEN octet_length(previous.payload::text) <= $4::bigint THEN previous.payload::text END
         FROM source_payloads AS previous
         CROSS JOIN LATERAL (
             SELECT EXISTS (SELECT 1 FROM measurements WHERE source_ref=previous.id)
         ) AS owned
         WHERE previous.id!=$1 AND previous.source='garmin_connect'
           AND previous.endpoint=$2 AND previous.source_key=$3
           AND (previous.status IN ('normalized','partial') OR owned.exists)
         LIMIT 1001",
    )
    .bind(raw.id)
    .bind(&raw.endpoint)
    .bind(&raw.source_key)
    .bind(i64::try_from(max_bytes).unwrap_or(i64::MAX))
    .fetch(&mut **txn);
    let mut count = 0;
    while let Some((owned, payload)) = older.try_next().await? {
        count += 1;
        if owned || count > MAX_APPLICATIONS {
            return Ok(true);
        }
        let payload = payload
            .and_then(|text| serde_json::from_str(&text).ok())
            .ok_or(ReplayInputError::HistoricalContract)?;
        let projection = normalize_health_payload(&raw.endpoint, &raw.source_key, &payload)
            .map_err(|_| ReplayInputError::HistoricalContract)?;
        if !projection.samples.is_empty() {
            return Ok(true);
        }
    }
    Ok(false)
}

fn channels(endpoint: &str) -> &[&str] {
    match endpoint {
        "heart_rate" => &["heart_rate_bpm"],
        "stress" => &["stress_score", "body_battery"],
        "hrv" => &["hrv_rmssd_ms"],
        "respiration" => &["respiration_rpm"],
        "spo2" => &["spo2_pct"],
        "steps" => &["steps_bucket"],
        _ => &[],
    }
}

fn read_replacement(
    value: &Value,
    endpoint: &str,
) -> Result<Option<ReplayReplacement>, ReplayInputError> {
    if value.is_null() || value.as_object().is_some_and(serde_json::Map::is_empty) {
        return Ok(None);
    }
    let error = ReplayInputError::HistoricalContract;
    let start = parse_clock(&value["start"]).map_err(|_| ReplayInputError::HistoricalContract)?;
    let end = parse_clock(&value["end"]).map_err(|_| ReplayInputError::HistoricalContract)?;
    let interval = end.unix_microseconds() - start.unix_microseconds();
    if !(1..=MAX_INTERVAL_MICROSECONDS).contains(&interval) {
        return Err(error);
    }
    let metrics: BTreeSet<&str> = value["metrics"]
        .as_array()
        .filter(|metrics| !metrics.is_empty())
        .and_then(|metrics| {
            metrics
                .iter()
                .map(|metric| {
                    metric
                        .as_str()
                        .filter(|name| channels(endpoint).contains(name))
                })
                .collect::<Option<_>>()
        })
        .ok_or(ReplayInputError::HistoricalContract)?;
    let evidence = value["evidence"]
        .as_str()
        .filter(|value| (1..=200).contains(&value.chars().count()))
        .ok_or(ReplayInputError::HistoricalContract)?;
    Ok(Some(ReplayReplacement {
        start,
        end,
        metrics: metrics.into_iter().map(str::to_owned).collect(),
        evidence: evidence.to_owned(),
    }))
}
