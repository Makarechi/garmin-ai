//! Immutable prospective hypothesis specifications.

use std::{error::Error, fmt};

use serde_json::{Value, json};
use speedate::{DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig};
use time::{Date, Month};
use uuid::Uuid;

use crate::event_input::known_timezone;

pub struct HypothesisSpec {
    document: Value,
    id: Uuid,
    discovery_start: Date,
    discovery_end: Date,
    validation_start: Date,
    validation_end: Date,
    expires: Date,
}

#[derive(Clone, Copy, Debug)]
pub struct InvalidHypothesisSpec;

impl fmt::Display for InvalidHypothesisSpec {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Hypothesis specification is invalid")
    }
}
impl Error for InvalidHypothesisSpec {}

impl HypothesisSpec {
    pub fn from_json(value: &Value) -> Result<Self, InvalidHypothesisSpec> {
        let fields = value.as_object().ok_or(InvalidHypothesisSpec)?;
        if fields.keys().any(|key| {
            !matches!(
                key.as_str(),
                "id" | "question"
                    | "discovery_start"
                    | "discovery_end"
                    | "validation_start"
                    | "validation_end"
                    | "expires"
                    | "timezone"
                    | "outcome"
                    | "late_hours"
                    | "direction"
            )
        }) {
            return Err(InvalidHypothesisSpec);
        }
        let id = value["id"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .ok_or(InvalidHypothesisSpec)?;
        let question = value["question"].as_str().ok_or(InvalidHypothesisSpec)?;
        let question_length = question.chars().count();
        if !(1..=1000).contains(&question_length)
            || question
                .chars()
                .all(|value| value.is_whitespace() || matches!(value, '\u{1c}'..='\u{1f}'))
        {
            return Err(InvalidHypothesisSpec);
        }
        let timezone = value["timezone"]
            .as_str()
            .filter(|zone| hypothesis_timezone(zone).is_some())
            .ok_or(InvalidHypothesisSpec)?;
        let outcome = fields
            .get("outcome")
            .map_or(Some("sleep_score"), Value::as_str)
            .filter(|outcome| matches!(*outcome, "sleep_score" | "sleep_seconds"))
            .ok_or(InvalidHypothesisSpec)?;
        let direction = value["direction"]
            .as_str()
            .filter(|direction| matches!(*direction, "lower" | "higher"))
            .ok_or(InvalidHypothesisSpec)?;
        let hours = fields
            .get("late_hours")
            .map_or(Some(6.0), float_argument)
            .filter(|hours| hours.is_finite() && *hours > 0.0 && *hours <= 24.0)
            .ok_or(InvalidHypothesisSpec)?;
        let discovery_start =
            date_argument(&value["discovery_start"]).ok_or(InvalidHypothesisSpec)?;
        let discovery_end = date_argument(&value["discovery_end"]).ok_or(InvalidHypothesisSpec)?;
        let validation_start =
            date_argument(&value["validation_start"]).ok_or(InvalidHypothesisSpec)?;
        let validation_end =
            date_argument(&value["validation_end"]).ok_or(InvalidHypothesisSpec)?;
        let expires = date_argument(&value["expires"]).ok_or(InvalidHypothesisSpec)?;
        let discovery_days = (discovery_end - discovery_start).whole_days();
        let validation_days = (validation_end - validation_start).whole_days();
        let expiry_days = (expires - validation_end).whole_days();
        if !(0..=30).contains(&discovery_days)
            || !(27..=30).contains(&validation_days)
            || !(2..=31).contains(&expiry_days)
            || discovery_end >= validation_start
        {
            return Err(InvalidHypothesisSpec);
        }
        let document = json!({
            "id":id.to_string(),"question":question,
            "discovery_start":discovery_start.to_string(),"discovery_end":discovery_end.to_string(),
            "validation_start":validation_start.to_string(),"validation_end":validation_end.to_string(),
            "expires":expires.to_string(),"timezone":timezone,"outcome":outcome,
            "late_hours":hours,"direction":direction,
        });
        Ok(Self {
            document,
            id,
            discovery_start,
            discovery_end,
            validation_start,
            validation_end,
            expires,
        })
    }

    pub fn document(&self) -> &Value {
        &self.document
    }
    pub fn matches(&self, stored: &Value) -> bool {
        crate::event_schema::python_equal(&self.document, stored)
    }
    pub fn id(&self) -> Uuid {
        self.id
    }
    pub fn discovery_period(&self) -> (Date, Date) {
        (self.discovery_start, self.discovery_end)
    }
    pub fn validation_period(&self) -> (Date, Date) {
        (self.validation_start, self.validation_end)
    }
    pub fn expires(&self) -> Date {
        self.expires
    }

    pub fn local_date(&self, at: crate::timestamp::StoredInstant) -> Option<Date> {
        let zone = hypothesis_timezone(self.document["timezone"].as_str()?)?;
        let utc = time::OffsetDateTime::from(at).to_offset(time::UtcOffset::UTC);
        let timestamp = match jiff::Timestamp::from_microsecond(at.unix_microseconds()) {
            Ok(timestamp) => timestamp,
            Err(_) if utc.year() == 9999 => {
                // Jiff reserves room for local offsets at its upper timestamp bound.
                // IANA's future POSIX rules repeat over the Gregorian 400-year cycle.
                let shifted = utc.replace_year(9599).ok()?;
                let micros = i64::try_from(shifted.unix_timestamp_nanos() / 1000).ok()?;
                jiff::Timestamp::from_microsecond(micros).ok()?
            }
            Err(_) => return None,
        };
        let offset =
            time::UtcOffset::from_whole_seconds(zone.to_offset(timestamp).seconds()).ok()?;
        let local = utc.checked_to_offset(offset)?;
        (1..=9999).contains(&local.year()).then_some(local.date())
    }
}

pub fn classify_validation(direction: &str, discovery: &Value, result: &Value) -> &'static str {
    if result["status"] == "insufficient_evidence" {
        return "insufficient_evidence";
    }
    let Some(interval) = confidence_interval(result) else {
        return "not_distinguished";
    };
    if supports_direction(direction, interval) {
        if discovery["status"] != "insufficient_evidence"
            && confidence_interval(discovery)
                .is_some_and(|interval| supports_direction(direction, interval))
        {
            "direction_repeated_observationally"
        } else {
            "validation_direction_supported_observationally"
        }
    } else if interval[1].as_f64().is_some_and(|value| value < 0.0)
        || interval[0].as_f64().is_some_and(|value| value > 0.0)
    {
        "opposite_direction"
    } else {
        "not_distinguished"
    }
}

fn confidence_interval(result: &Value) -> Option<&[Value]> {
    result["comparison"]["ci95"]
        .as_array()
        .filter(|values| values.len() >= 2)
        .map(Vec::as_slice)
}

fn supports_direction(direction: &str, interval: &[Value]) -> bool {
    match direction {
        "lower" => interval[1].as_f64().is_some_and(|value| value < 0.0),
        "higher" => interval[0].as_f64().is_some_and(|value| value > 0.0),
        _ => false,
    }
}

fn hypothesis_timezone(name: &str) -> Option<jiff::tz::TimeZone> {
    let base = name
        .strip_prefix("posix/")
        .or_else(|| name.strip_prefix("right/"))
        .unwrap_or(name);
    if !known_timezone(base) {
        return None;
    }
    let zone = jiff::tz::db().get(base).ok()?;
    if !name.starts_with("right/") || zone.to_fixed_offset().is_ok() {
        return Some(zone);
    }
    // ZoneInfo ignores leap records but retains the right/ file's transition table.
    jiff::tz::TimeZoneDatabase::from_dir("/usr/share/zoneinfo/right")
        .ok()?
        .get(base)
        .ok()
}

fn float_argument(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(value) => Some(f64::from(u8::from(*value))),
        Value::String(value) => {
            let text = value.trim();
            let bytes = text.as_bytes();
            for (index, byte) in bytes.iter().enumerate() {
                if *byte == b'_'
                    && (index == 0
                        || index + 1 == bytes.len()
                        || !bytes[index - 1].is_ascii_digit()
                        || !bytes[index + 1].is_ascii_digit())
                {
                    return None;
                }
            }
            text.replace('_', "").parse().ok()
        }
        value => value.as_f64(),
    }
}

fn date_argument(value: &Value) -> Option<Date> {
    let bytes = match value {
        Value::String(value) => value.as_bytes(),
        Value::Number(value) => value.as_str().as_bytes(),
        _ => return None,
    };
    let parsed = speedate::Date::parse_bytes(bytes).ok().or_else(|| {
        let config = DateTimeConfig {
            time_config: TimeConfig {
                microseconds_precision_overflow_behavior:
                    MicrosecondsPrecisionOverflowBehavior::Truncate,
                ..TimeConfig::default()
            },
            ..DateTimeConfig::default()
        };
        let stamp = DateTime::parse_bytes_with_config(bytes, &config).ok()?;
        (stamp.time.hour == 0
            && stamp.time.minute == 0
            && stamp.time.second == 0
            && stamp.time.microsecond == 0)
            .then_some(stamp.date)
    })?;
    if parsed.year == 0 {
        return None;
    }
    Date::from_calendar_date(
        i32::from(parsed.year),
        Month::try_from(parsed.month).ok()?,
        parsed.day,
    )
    .ok()
}

#[cfg(test)]
mod tests {
    use serde_json::Value;
    use std::error::Error;

    #[test]
    fn matches_frozen_recheck_decisions() -> Result<(), Box<dyn Error>> {
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../contracts/hypothesis-conclusions.json"))?;
        for (index, case) in cases.iter().enumerate() {
            let actual = super::classify_validation(
                case["direction"].as_str().ok_or("Missing direction")?,
                &case["discovery"],
                &case["result"],
            );
            assert_eq!(
                Some(actual),
                case["conclusion"].as_str(),
                "Conclusion {index}"
            );
        }
        Ok(())
    }

    #[test]
    fn matches_frozen_hypothesis_specifications() -> Result<(), Box<dyn Error>> {
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../contracts/hypothesis-specs.json"))?;
        for (index, case) in cases.iter().enumerate() {
            let actual = super::HypothesisSpec::from_json(&case["input"]);
            if case["error"].is_string() {
                assert!(actual.is_err(), "Rejected specification {index}");
            } else {
                let actual = actual.map_err(|_| format!("Unexpected rejection {index}"))?;
                assert_eq!(
                    actual.document(),
                    &case["normalized"],
                    "Normalized specification {index}"
                );
                let registered = super::HypothesisSpec::from_json(actual.document())?;
                assert_eq!(
                    registered.document(),
                    &case["registration_normalized"],
                    "Registered specification {index}"
                );
                for clock in case["local_dates"]
                    .as_array()
                    .ok_or("Missing local dates")?
                {
                    let at: crate::timestamp::StoredInstant =
                        clock["clock"].as_str().ok_or("Missing clock")?.parse()?;
                    let local = actual.local_date(at);
                    if clock["error"].is_string() {
                        assert!(local.is_none(), "Calendar overflow {index}");
                    } else {
                        assert_eq!(
                            local.map(|date| date.to_string()).as_deref(),
                            clock["date"].as_str(),
                            "Local date {index}"
                        );
                    }
                }
            }
        }
        Ok(())
    }
}
