//! Immutable prospective hypothesis specifications.

use std::{error::Error, fmt};

use serde_json::{Value, json};
use time::Date;
use uuid::Uuid;

use crate::timestamp::python_timezone;

mod validation;

pub struct HypothesisSpec {
    document: Value,
    id: Uuid,
    discovery_start: Date,
    discovery_end: Date,
    validation_start: Date,
    validation_end: Date,
    expires: Date,
}

pub struct InvalidHypothesisSpec(Vec<Value>);

impl InvalidHypothesisSpec {
    pub fn details(self) -> Vec<Value> {
        self.0
    }
}

impl fmt::Debug for InvalidHypothesisSpec {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("InvalidHypothesisSpec")
            .field("issues", &self.0.len())
            .finish()
    }
}

impl fmt::Display for InvalidHypothesisSpec {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Hypothesis specification is invalid")
    }
}
impl Error for InvalidHypothesisSpec {}

impl HypothesisSpec {
    pub fn parse_path_identity(value: &str) -> Result<Uuid, InvalidHypothesisSpec> {
        validation::path_id(value)
    }

    pub fn from_json(value: &Value) -> Result<Self, InvalidHypothesisSpec> {
        let mut body = validation::Body::new(value)?;
        let id = body.required("id", validation::id);
        let question = body.required("question", validation::question);
        let discovery_start = body.required("discovery_start", validation::date);
        let discovery_end = body.required("discovery_end", validation::date);
        let validation_start = body.required("validation_start", validation::date);
        let validation_end = body.required("validation_end", validation::date);
        let expires = body.required("expires", validation::date);
        let timezone = body.required("timezone", validation::text);
        let outcome = body.optional("outcome", "sleep_score", |input| {
            validation::literal(input, ["sleep_score", "sleep_seconds"])
        });
        let hours = body.optional("late_hours", 6.0, validation::hours);
        let direction = body.required("direction", |input| {
            validation::literal(input, ["lower", "higher"])
        });
        body.check_fields()?;
        let (
            Some(id),
            Some(question),
            Some(discovery_start),
            Some(discovery_end),
            Some(validation_start),
            Some(validation_end),
            Some(expires),
            Some(timezone),
            Some(outcome),
            Some(hours),
            Some(direction),
        ) = (
            id,
            question,
            discovery_start,
            discovery_end,
            validation_start,
            validation_end,
            expires,
            timezone,
            outcome,
            hours,
            direction,
        )
        else {
            return Err(InvalidHypothesisSpec(Vec::new()));
        };

        let invalid = |reason| validation::invalid_protocol(reason, value);
        if python_timezone(timezone).is_none() {
            return Err(invalid("Unknown timezone"));
        }
        let discovery_days = (discovery_end - discovery_start).whole_days();
        let validation_days = (validation_end - validation_start).whole_days();
        if !(0..=30).contains(&discovery_days) || !(0..=30).contains(&validation_days) {
            return Err(invalid("Date range must be ordered and at most 30 days"));
        }
        if validation_days < 27 {
            return Err(invalid(
                "Directional validation requires at least 28 inclusive dates",
            ));
        }
        if discovery_end >= validation_start || validation_end >= expires {
            return Err(invalid(
                "Discovery, prospective validation and expiry must be ordered",
            ));
        }
        let expiry_days = (expires - validation_end).whole_days();
        if !(2..=31).contains(&expiry_days)
            || question
                .chars()
                .all(|value| value.is_whitespace() || matches!(value, '\u{1c}'..='\u{1f}'))
        {
            return Err(invalid("Expiry must be 2 to 31 days after validation end"));
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
        let zone = python_timezone(self.document["timezone"].as_str()?)?;
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
