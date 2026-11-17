use std::{error::Error, fmt};

use serde_json::{Map, Value, json};
use speedate::{Date, DateTime, MicrosecondsPrecisionOverflowBehavior, TimeConfig};

use crate::{event_input::known_timezone, timestamp::StoredInstant};

const EPOCH_MILLISECOND_THRESHOLD: f64 = 100_000_000_000.0;
const GMT_TIME: TimeConfig = TimeConfig {
    microseconds_precision_overflow_behavior: MicrosecondsPrecisionOverflowBehavior::Truncate,
    unix_timestamp_offset: Some(0),
};

const METRIC_FIELDS: &[(&str, &[&str])] = &[
    ("duration_seconds", &["duration"]),
    ("moving_seconds", &["movingDuration"]),
    ("distance_m", &["distance"]),
    ("avg_hr", &["averageHR"]),
    ("max_hr", &["maxHR"]),
    ("avg_speed_mps", &["averageSpeed"]),
    ("calories", &["calories"]),
    (
        "cadence",
        &["averageRunningCadenceInStepsPerMinute", "averageRunCadence"],
    ),
    ("ascent_m", &["elevationGain"]),
    ("descent_m", &["elevationLoss"]),
    (
        "aerobic_effect",
        &["aerobicTrainingEffect", "trainingEffect"],
    ),
    ("anaerobic_effect", &["anaerobicTrainingEffect"]),
    ("training_load", &["activityTrainingLoad"]),
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvalidGarminSummary {
    Identity,
    Shape,
    Start,
    Duration,
    Timezone,
}

impl fmt::Display for InvalidGarminSummary {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Identity => "Garmin activity identity is invalid",
            Self::Shape => "Garmin activity summary is malformed",
            Self::Start => "Garmin activity lacks a valid GMT start",
            Self::Duration => "Garmin activity lacks a valid duration",
            Self::Timezone => "Garmin activity timezone is invalid",
        })
    }
}

impl Error for InvalidGarminSummary {}

pub fn activity_identity(document: &Value) -> Result<String, InvalidGarminSummary> {
    match document.get("activityId") {
        Some(Value::String(value)) if !value.is_empty() => Ok(value.clone()),
        Some(Value::Number(value)) => Ok(value.to_string()),
        _ => Err(InvalidGarminSummary::Identity),
    }
}

/// Interpret Garmin start times for fields explicitly designated GMT. Epoch
/// numbers use the legacy seconds/milliseconds threshold.
pub fn gmt_instant(value: &Value) -> Option<StoredInstant> {
    let micros = match value {
        Value::Number(number) => {
            let epoch = number.as_f64()?;
            let seconds = if epoch.abs() > EPOCH_MILLISECOND_THRESHOLD {
                epoch / 1000.0
            } else {
                epoch
            };
            let integral = seconds.trunc();
            (integral as i64)
                .checked_mul(1_000_000)?
                .checked_add((seconds.fract() * 1_000_000.0).round_ties_even() as i64)?
        }
        Value::String(text) => {
            match DateTime::parse_bytes_rfc3339_with_config(text.as_bytes(), &GMT_TIME) {
                Ok(date_time) => date_time
                    .timestamp_tz()
                    .checked_mul(1_000_000)?
                    .checked_add(i64::from(date_time.time.microsecond))?,
                Err(_) if text.len() == 10 => {
                    let date = Date::parse_bytes(text.as_bytes()).ok()?;
                    date.timestamp().checked_mul(1_000_000)?
                }
                Err(_) => return None,
            }
        }
        _ => return None,
    };
    StoredInstant::from_unix_microseconds(micros).ok()
}

/// Extract only the fields supplied by this response. Missing summary fields
/// keep their current value and ownership when applying a later observation.
pub fn summary_fields(document: &Value) -> Result<Map<String, Value>, InvalidGarminSummary> {
    let (fields, timing) = extract_fields(document)?;
    timing?;
    validate_timezone(&fields)?;
    Ok(fields)
}

/// A replay may repair only the fields this revision still owns. Missing
/// optional fields previously supplied by this revision can be cleared.
pub fn replay_summary_fields(
    document: &Value,
    preserve_timing: bool,
    owns: impl Fn(&str) -> bool,
) -> Result<Map<String, Value>, InvalidGarminSummary> {
    let (mut fields, timing) = extract_fields(document)?;
    if preserve_timing {
        fields.remove("start");
        fields.remove("end");
    } else {
        timing?;
    }
    validate_timezone(&fields)?;
    for key in METRIC_FIELDS.iter().map(|(name, _)| *name).chain(["name"]) {
        if owns(key) && !fields.contains_key(key) {
            fields.insert(key.to_owned(), Value::Null);
        }
    }
    Ok(fields)
}

fn validate_timezone(fields: &Map<String, Value>) -> Result<(), InvalidGarminSummary> {
    if fields
        .get("timezone")
        .and_then(Value::as_str)
        .is_some_and(|zone| !known_timezone(zone))
    {
        return Err(InvalidGarminSummary::Timezone);
    }
    Ok(())
}

/// Interpret fields from older retained entries even when their timing was incomplete.
pub fn historical_summary_fields(
    document: &Value,
) -> Result<Map<String, Value>, InvalidGarminSummary> {
    extract_fields(document).map(|(fields, _)| fields)
}

type FieldsAndTiming = (Map<String, Value>, Result<(), InvalidGarminSummary>);

fn extract_fields(document: &Value) -> Result<FieldsAndTiming, InvalidGarminSummary> {
    let origin = document.as_object().ok_or(InvalidGarminSummary::Shape)?;
    let nested = nested_object(origin.get("summaryDTO"))?;
    let find = |key: &str| {
        nested
            .and_then(|nested| nested.get(key))
            .or_else(|| origin.get(key))
    };
    let mut fields = Map::new();
    let timing = summary_timing(find).map(|(start, end)| {
        fields.insert("start".to_owned(), json!(start.to_string()));
        fields.insert("end".to_owned(), json!(end.to_string()));
    });
    for (name, aliases) in METRIC_FIELDS {
        if let Some(value) = aliases
            .iter()
            .find_map(|key| find(key).and_then(nonnegative))
        {
            fields.insert((*name).to_owned(), json!(value));
        }
    }
    let kind = match nested_object(origin.get("activityType"))? {
        Some(kind) => Some(kind),
        None => nested_object(origin.get("activityTypeDTO"))?,
    }
    .and_then(|kind| kind.get("typeKey"));
    let timezone =
        nested_object(origin.get("timeZoneUnitDTO"))?.and_then(|timezone| timezone.get("timeZone"));
    store_text(&mut fields, "kind", kind, true)?;
    store_text(&mut fields, "name", origin.get("activityName"), false)?;
    store_text(&mut fields, "timezone", timezone, true)?;
    Ok((fields, timing))
}

fn summary_timing<'a>(
    find: impl Fn(&str) -> Option<&'a Value>,
) -> Result<(StoredInstant, StoredInstant), InvalidGarminSummary> {
    let start = find("startTimeGMT")
        .filter(|value| value.as_f64() != Some(0.0))
        .and_then(gmt_instant)
        .ok_or(InvalidGarminSummary::Start)?;
    let duration = find("duration")
        .and_then(nonnegative)
        .ok_or(InvalidGarminSummary::Duration)?;
    let elapsed = find("elapsedDuration")
        .and_then(nonnegative)
        .filter(|value| *value != 0.0)
        .unwrap_or(duration);
    let end = start
        .unix_microseconds()
        .checked_add((elapsed * 1_000_000.0).round_ties_even() as i64)
        .and_then(|micros| StoredInstant::from_unix_microseconds(micros).ok())
        .ok_or(InvalidGarminSummary::Duration)?;

    Ok((start, end))
}

fn nonnegative(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .filter(|value| value.is_finite() && *value >= 0.0)
}

fn nested_object(
    value: Option<&Value>,
) -> Result<Option<&Map<String, Value>>, InvalidGarminSummary> {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Ok(None),
        Some(Value::Object(fields)) => Ok(Some(fields).filter(|fields| !fields.is_empty())),
        _ => Err(InvalidGarminSummary::Shape),
    }
}

fn store_text(
    fields: &mut Map<String, Value>,
    key: &str,
    value: Option<&Value>,
    omit_empty: bool,
) -> Result<(), InvalidGarminSummary> {
    match value {
        Some(Value::String(value)) if !omit_empty || !value.is_empty() => {
            fields.insert(key.to_owned(), json!(value));
        }
        None | Some(Value::Null) | Some(Value::String(_)) => (),
        _ => return Err(InvalidGarminSummary::Shape),
    }
    Ok(())
}
