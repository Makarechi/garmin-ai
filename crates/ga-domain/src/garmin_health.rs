mod replacement;

pub use replacement::{HealthReplacement, InvalidHealthReplacement, replacement_channels};

use std::{error::Error, fmt};

use serde_json::{Map, Value, json};
use time::{Date, format_description::well_known::Iso8601};

use crate::{garmin_summary::gmt_instant, timestamp::StoredInstant};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HealthStatus {
    Normalized,
    Archived,
    Empty,
}

impl HealthStatus {
    pub fn label(self) -> &'static str {
        match self {
            Self::Normalized => "normalized",
            Self::Archived => "archived",
            Self::Empty => "empty",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidHealthPayload;

impl fmt::Display for InvalidHealthPayload {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Garmin health payload could not be interpreted")
    }
}
impl Error for InvalidHealthPayload {}

#[derive(Clone, Copy, Debug)]
pub struct Measurement {
    pub instant: StoredInstant,
    pub metric: &'static str,
    pub unit: &'static str,
    pub value: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct KnownObservation {
    pub metric: &'static str,
    pub unit: &'static str,
    pub value: f64,
    pub sequence: u32,
    pub observed: Option<StoredInstant>,
    pub interval: Option<(StoredInstant, StoredInstant)>,
}

#[derive(Debug)]
pub struct HealthProjection {
    pub status: HealthStatus,
    pub day: Option<Date>,
    pub fields: Map<String, Value>,
    pub samples: Vec<Measurement>,
    pub observations: Vec<KnownObservation>,
    pub sleep_interval: Option<(StoredInstant, StoredInstant)>,
}

#[derive(Clone, Copy)]
struct SampleSpec {
    metric: &'static str,
    unit: &'static str,
    minimum: f64,
    maximum: f64,
}

const HEART_RATE: SampleSpec = SampleSpec {
    metric: "heart_rate_bpm",
    unit: "bpm",
    minimum: 1.0,
    maximum: 300.0,
};
const STRESS: SampleSpec = SampleSpec {
    metric: "stress_score",
    unit: "score",
    minimum: 0.0,
    maximum: 100.0,
};
const BATTERY: SampleSpec = SampleSpec {
    metric: "body_battery",
    unit: "score",
    minimum: 0.0,
    maximum: 100.0,
};
const RESPIRATION: SampleSpec = SampleSpec {
    metric: "respiration_rpm",
    unit: "rpm",
    minimum: 1.0,
    maximum: 100.0,
};
const SPO2: SampleSpec = SampleSpec {
    metric: "spo2_pct",
    unit: "%",
    minimum: 1.0,
    maximum: 100.0,
};
const HRV: SampleSpec = SampleSpec {
    metric: "hrv_rmssd_ms",
    unit: "ms",
    minimum: 0.0,
    maximum: f64::MAX,
};
const STEPS: SampleSpec = SampleSpec {
    metric: "steps_bucket",
    unit: "steps",
    minimum: 0.0,
    maximum: f64::MAX,
};

/// Interpret the legacy endpoint response while preserving sample order and
/// distinguishing absent values from zero, quality gaps, and untimed evidence.
pub fn normalize_health_payload(
    endpoint: &str,
    key: &str,
    input: &Value,
) -> Result<HealthProjection, InvalidHealthPayload> {
    let empty = input.is_null()
        || input.as_object().is_some_and(Map::is_empty)
        || input.as_array().is_some_and(Vec::is_empty);
    let day = Date::parse(key, &Iso8601::DEFAULT).ok();
    let mut result = HealthProjection {
        status: if empty {
            HealthStatus::Empty
        } else {
            HealthStatus::Archived
        },
        day,
        fields: Map::new(),
        samples: Vec::new(),
        observations: Vec::new(),
        sleep_interval: None,
    };
    if empty || day.is_none() {
        return Ok(result);
    }

    match endpoint {
        "daily" => daily(&mut result, input)?,
        "sleep" => sleep(&mut result, input)?,
        "hrv" => hrv(&mut result, input)?,
        "readiness" => readiness(&mut result, input, key)?,
        "body_battery" => {
            for row in rows(input) {
                let row = object(row)?;
                if row.get("date").is_none_or(|day| day.as_str() == Some(key)) {
                    result.map_numbers(
                        row,
                        &[
                            ("body_battery_charged", "charged"),
                            ("body_battery_drained", "drained"),
                        ],
                    );
                }
            }
        }
        "heart_rate" | "stress" | "respiration" | "spo2" => series(&mut result, input, endpoint)?,
        "steps" => {
            for bucket in input.as_array().ok_or(InvalidHealthPayload)? {
                let bucket = object(bucket)?;
                result.add_sample(field(bucket, "startGMT"), field(bucket, "steps"), STEPS)?;
            }
        }
        "hydration" => result.map_numbers(object(input)?, &[("hydration_ml", "valueInML")]),
        "max_metrics" => {
            for row in rows(input) {
                let dto = nested(field(object(row)?, "generic"))?;
                result.map_numbers(dto, &[("vo2max", "vo2MaxPreciseValue")]);
            }
        }
        _ => return Ok(result),
    }
    result.status = HealthStatus::Normalized;
    Ok(result)
}

impl HealthProjection {
    fn map_numbers(&mut self, source: &Map<String, Value>, mapping: &[(&str, &str)]) {
        for (target, original) in mapping {
            match number(field(source, original), 0.0, f64::MAX) {
                Some(value) => {
                    self.fields.insert((*target).to_owned(), json!(value));
                }
                None => {
                    self.fields.remove(*target);
                }
            }
        }
    }

    fn add_sample(
        &mut self,
        ts: &Value,
        value: &Value,
        spec: SampleSpec,
    ) -> Result<(), InvalidHealthPayload> {
        if let Some(value) = number(value, spec.minimum, spec.maximum)
            && !ts.is_null()
        {
            let instant = gmt_instant(ts).ok_or(InvalidHealthPayload)?;
            self.samples.push(Measurement {
                instant,
                metric: spec.metric,
                unit: spec.unit,
                value,
            });
        }
        Ok(())
    }

    fn add_observation(
        &mut self,
        metric: &'static str,
        unit: &'static str,
        value: Option<f64>,
        sequence: u32,
        observed: Option<StoredInstant>,
        interval: Option<(StoredInstant, StoredInstant)>,
    ) {
        if let Some(value) = value {
            self.observations.push(KnownObservation {
                metric,
                unit,
                value,
                sequence,
                observed,
                interval,
            });
        }
    }
}

fn daily(result: &mut HealthProjection, input: &Value) -> Result<(), InvalidHealthPayload> {
    let input = object(input)?;
    result.map_numbers(
        input,
        &[
            ("steps", "totalSteps"),
            ("active_calories", "activeKilocalories"),
            ("resting_hr", "restingHeartRate"),
            ("stress_avg", "averageStressLevel"),
            ("stress_max", "maxStressLevel"),
            ("body_battery_high", "bodyBatteryHighestValue"),
            ("body_battery_low", "bodyBatteryLowestValue"),
            ("body_battery_charged", "bodyBatteryChargedValue"),
            ("body_battery_drained", "bodyBatteryDrainedValue"),
        ],
    );
    if let (Some(moderate), Some(vigorous)) = (
        number(field(input, "moderateIntensityMinutes"), 0.0, f64::MAX),
        number(field(input, "vigorousIntensityMinutes"), 0.0, f64::MAX),
    ) {
        let points = moderate + 2.0 * vigorous;
        if !points.is_finite() {
            return Err(InvalidHealthPayload);
        }
        result
            .fields
            .insert("intensity_minutes".to_owned(), json!(points));
    }
    Ok(())
}

fn sleep(result: &mut HealthProjection, input: &Value) -> Result<(), InvalidHealthPayload> {
    let dto = nested(field(object(input)?, "dailySleepDTO"))?;
    result.map_numbers(
        dto,
        &[
            ("sleep_seconds", "sleepTimeSeconds"),
            ("deep_seconds", "deepSleepSeconds"),
            ("rem_seconds", "remSleepSeconds"),
            ("light_seconds", "lightSleepSeconds"),
            ("awake_seconds", "awakeSleepSeconds"),
        ],
    );
    let score = number(
        field(
            nested(field(nested(field(dto, "sleepScores"))?, "overall"))?,
            "value",
        ),
        0.0,
        100.0,
    );
    if let Some(score) = score {
        result.fields.insert("sleep_score".to_owned(), json!(score));
    }
    let (start, end) = (
        field(dto, "sleepStartTimestampGMT"),
        field(dto, "sleepEndTimestampGMT"),
    );
    if !start.is_null() && !end.is_null() {
        let start = gmt_instant(start).ok_or(InvalidHealthPayload)?;
        let end = gmt_instant(end).ok_or(InvalidHealthPayload)?;
        if end > start {
            result.sleep_interval = Some((start, end));
            result.add_observation(
                "sleep_score",
                "score",
                score,
                0,
                Some(end),
                Some((start, end)),
            );
        }
    }
    Ok(())
}

fn hrv(result: &mut HealthProjection, input: &Value) -> Result<(), InvalidHealthPayload> {
    let input = object(input)?;
    let dto = nested(field(input, "hrvSummary"))?;
    result.map_numbers(
        dto,
        &[
            ("hrv_nightly_avg", "lastNightAvg"),
            ("hrv_weekly_avg", "weeklyAvg"),
        ],
    );
    result.map_numbers(
        nested(field(dto, "baseline"))?,
        &[
            ("hrv_baseline_low", "balancedLow"),
            ("hrv_baseline_high", "balancedUpper"),
        ],
    );
    if !field(dto, "status").is_null() {
        let status = field(dto, "status").as_str().ok_or(InvalidHealthPayload)?;
        result.fields.insert("hrv_status".to_owned(), json!(status));
    }
    for reading in sequence(field(input, "hrvReadings"))? {
        let reading = object(reading)?;
        result.add_sample(
            field(reading, "readingTimeGMT"),
            field(reading, "hrvValue"),
            HRV,
        )?;
    }
    Ok(())
}

fn readiness(
    result: &mut HealthProjection,
    input: &Value,
    key: &str,
) -> Result<(), InvalidHealthPayload> {
    let mut latest: Option<(String, Option<f64>, Option<f64>)> = None;
    let mut index = 0;
    for entry in rows(input) {
        let entry = object(entry)?;
        if entry
            .get("calendarDate")
            .is_some_and(|day| day.as_str() != Some(key))
        {
            continue;
        }
        let value = number(field(entry, "score"), 0.0, 100.0);
        let recovery = if field(entry, "recoveryTimeChangePhrase") == "REACHED_ZERO" {
            Some(0.0)
        } else {
            number(field(entry, "recoveryTime"), 0.0, f64::MAX)
        };
        let stamp = field(entry, "timestamp");
        let observed = stamp.as_str().and_then(explicit_instant);
        result.add_observation(
            "training_readiness_score",
            "score",
            value,
            index,
            observed,
            None,
        );
        result.add_observation(
            "recovery_time_minutes",
            "minutes",
            recovery,
            index,
            observed,
            None,
        );
        index = index.checked_add(1).ok_or(InvalidHealthPayload)?;
        let sort_stamp = match stamp {
            Value::Null | Value::Bool(false) => String::new(),
            Value::String(text) => text.clone(),
            Value::Bool(true) => "True".to_owned(),
            value => value.to_string(),
        };
        if latest
            .as_ref()
            .is_none_or(|(last, _, _)| sort_stamp > *last)
        {
            latest = Some((sort_stamp, value, recovery));
        }
    }
    if let Some((_, value, recovery)) = latest {
        for (key, value) in [
            ("training_readiness_score", value),
            ("recovery_time_minutes", recovery),
        ] {
            if let Some(value) = value {
                result.fields.insert(key.to_owned(), json!(value));
            }
        }
    }
    Ok(())
}

fn series(
    result: &mut HealthProjection,
    input: &Value,
    endpoint: &str,
) -> Result<(), InvalidHealthPayload> {
    let input = object(input)?;
    let (key, spec) = match endpoint {
        "heart_rate" => ("heartRateValues", HEART_RATE),
        "stress" => ("stressValuesArray", STRESS),
        "respiration" => ("respirationValuesArray", RESPIRATION),
        "spo2" if input.contains_key("spO2HourlyAverages") => ("spO2HourlyAverages", SPO2),
        "spo2" => ("spO2ValuesArray", SPO2),
        _ => return Err(InvalidHealthPayload),
    };
    for entry in sequence(field(input, key))? {
        if let Some(point) = entry.as_array()
            && let [time, value, ..] = point.as_slice()
        {
            result.add_sample(time, value, spec)?;
        }
    }
    if endpoint == "heart_rate"
        && let Some(resting) = number(field(input, "restingHeartRate"), 1.0, 300.0)
    {
        result
            .fields
            .insert("resting_hr".to_owned(), json!(resting));
    }
    if endpoint == "stress" {
        body_battery(result, input)?;
    }
    Ok(())
}

fn body_battery(
    result: &mut HealthProjection,
    input: &Map<String, Value>,
) -> Result<(), InvalidHealthPayload> {
    for descriptor in sequence(field(input, "bodyBatteryValueDescriptorsDTOList"))? {
        let descriptor = object(descriptor)?;
        if field(descriptor, "bodyBatteryValueDescriptorKey") != "bodyBatteryLevel" {
            continue;
        }
        let ordinal = field(descriptor, "bodyBatteryValueDescriptorIndex");
        let index = ordinal
            .as_i64()
            .or_else(|| ordinal.as_f64().map(|value| value as i64))
            .or_else(|| ordinal.as_bool().map(i64::from))
            .or_else(|| ordinal.as_str().and_then(|value| value.trim().parse().ok()))
            .ok_or(InvalidHealthPayload)?;
        for point in sequence(field(input, "bodyBatteryValuesArray"))? {
            let point = point.as_array().ok_or(InvalidHealthPayload)?;
            let size = i64::try_from(point.len()).map_err(|_| InvalidHealthPayload)?;
            if size > index {
                let index = if index < 0 { size + index } else { index };
                let index = usize::try_from(index).map_err(|_| InvalidHealthPayload)?;
                result.add_sample(
                    point.first().ok_or(InvalidHealthPayload)?,
                    point.get(index).ok_or(InvalidHealthPayload)?,
                    BATTERY,
                )?;
            }
        }
        break;
    }
    Ok(())
}

fn explicit_instant(text: &str) -> Option<StoredInstant> {
    let parsed = speedate::DateTime::parse_bytes_rfc3339_with_config(
        text.as_bytes(),
        &speedate::TimeConfig {
            microseconds_precision_overflow_behavior:
                speedate::MicrosecondsPrecisionOverflowBehavior::Truncate,
            unix_timestamp_offset: None,
        },
    )
    .ok()?;
    parsed.time.tz_offset?;
    let micros = parsed
        .timestamp_tz()
        .checked_mul(1_000_000)?
        .checked_add(i64::from(parsed.time.microsecond))?;
    StoredInstant::from_unix_microseconds(micros).ok()
}

fn field<'a>(object: &'a Map<String, Value>, key: &str) -> &'a Value {
    object.get(key).unwrap_or(&Value::Null)
}

fn object(value: &Value) -> Result<&Map<String, Value>, InvalidHealthPayload> {
    value.as_object().ok_or(InvalidHealthPayload)
}

fn nested(value: &Value) -> Result<&Map<String, Value>, InvalidHealthPayload> {
    static EMPTY: std::sync::LazyLock<Map<String, Value>> = std::sync::LazyLock::new(Map::new);
    match value {
        Value::Null | Value::Bool(false) => Ok(&EMPTY),
        value => object(value),
    }
}

fn sequence(value: &Value) -> Result<&[Value], InvalidHealthPayload> {
    match value {
        Value::Null | Value::Bool(false) => Ok(&[]),
        value => value
            .as_array()
            .map(Vec::as_slice)
            .ok_or(InvalidHealthPayload),
    }
}

fn rows(input: &Value) -> &[Value] {
    input
        .as_array()
        .map_or_else(|| std::slice::from_ref(input), Vec::as_slice)
}

fn number(value: &Value, minimum: f64, maximum: f64) -> Option<f64> {
    value
        .as_f64()
        .filter(|value| (minimum..=maximum).contains(value))
}
