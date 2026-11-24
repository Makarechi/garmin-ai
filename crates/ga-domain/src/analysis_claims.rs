//! Verify model citations against values returned by trusted read tools.

use std::fmt::Write;
use std::{collections::BTreeSet, error::Error, fmt};

use jiff::Timestamp;
use num_bigint::{BigInt, ToBigInt};
use serde_json::{Number, Value};
use speedate::{DateTime, MicrosecondsPrecisionOverflowBehavior, TimeConfig};

use crate::{event_time, localization::catalog_text};

#[derive(Clone, Copy)]
pub struct AnswerEvidence<'a> {
    pub id: u64,
    pub tool: &'a str,
    pub result: &'a Value,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClaimError {
    InvalidClaim,
    MissingEvidence,
    MissingPath,
    DifferentValue,
    InvalidTimestamp,
}

impl fmt::Display for ClaimError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidClaim => "An analysis claim has invalid fields",
            Self::MissingEvidence => "A claim requires selected and successful evidence",
            Self::MissingPath => "The cited field does not exist in the selected evidence",
            Self::DifferentValue => "The claim differs from the cited value",
            Self::InvalidTimestamp => {
                "The cited timestamp requires a valid local time and UTC offset"
            }
        })
    }
}
impl Error for ClaimError {}

/// Format only numbers verified at exactly the selected result paths.
pub fn verified_numbers(
    claims: &[Value],
    evidence: &[AnswerEvidence<'_>],
    selected: &BTreeSet<u64>,
) -> Result<Vec<String>, ClaimError> {
    if claims.len() > 20 {
        return Err(ClaimError::InvalidClaim);
    }
    claims
        .iter()
        .map(|claim| {
            let requested = claim["value"].as_number().ok_or(ClaimError::InvalidClaim)?;
            let (item, path, observed) = cited_value(claim, evidence, selected)?;
            let observed = observed.as_number().ok_or(ClaimError::DifferentValue)?;
            let actual = ObservedNumber::from_json(observed).ok_or(ClaimError::DifferentValue)?;
            let requested = ObservedNumber::from_json(requested).ok_or(ClaimError::InvalidClaim)?;
            if !actual.matches(&requested) {
                return Err(ClaimError::DifferentValue);
            }
            Ok(format!(
                "Evidence {}: {} {}: {}",
                item.id,
                item.tool,
                evidence_path(path)?,
                actual.render(),
            ))
        })
        .collect()
}

/// Verify the exact timestamp string and render it in the owner's timezone.
pub fn verified_timestamps(
    claims: &[Value],
    evidence: &[AnswerEvidence<'_>],
    selected: &BTreeSet<u64>,
    timezone: &str,
    locale: &str,
) -> Result<Vec<String>, ClaimError> {
    if claims.len() > 20 {
        return Err(ClaimError::InvalidClaim);
    }
    let zone = jiff::tz::db()
        .get(timezone)
        .map_err(|_| ClaimError::InvalidTimestamp)?;
    let language = locale.split('-').next().unwrap_or_default();
    claims
        .iter()
        .map(|claim| {
            let requested = claim["value"]
                .as_str()
                .filter(|value| (1..=80).contains(&value.chars().count()))
                .ok_or(ClaimError::InvalidClaim)?;
            let (item, path, observed) = cited_value(claim, evidence, selected)?;
            if observed.as_str() != Some(requested) {
                return Err(ClaimError::DifferentValue);
            }
            let instant = cited_timestamp(requested)?;
            let local = instant.to_zoned_datetime(zone.clone());
            if !(1..=9999).contains(&local.year()) {
                return Err(ClaimError::InvalidTimestamp);
            }
            let clock = if language == "ru" {
                local.strftime("%d.%m.%Y в %H:%M:%S").to_string()
            } else {
                local.strftime("%Y-%m-%d %H:%M:%S").to_string()
            };
            let label = match sleep_label(item.tool, path, language) {
                Some(label) => label.to_owned(),
                None => format!(
                    "Evidence {}: {} {}",
                    item.id,
                    item.tool,
                    evidence_path(path)?
                ),
            };
            let seconds = local.offset().seconds();
            let magnitude = seconds.unsigned_abs();
            let sign = if seconds < 0 { '-' } else { '+' };
            let mut offset = format!("{sign}{:02}{:02}", magnitude / 3600, magnitude % 3600 / 60);
            if magnitude % 60 != 0 {
                let _ = write!(offset, "{:02}", magnitude % 60);
            }
            Ok(format!("{label}: {clock} ({timezone}, {offset})."))
        })
        .collect()
}

fn cited_timestamp(value: &str) -> Result<Timestamp, ClaimError> {
    const TIME: TimeConfig = TimeConfig {
        microseconds_precision_overflow_behavior: MicrosecondsPrecisionOverflowBehavior::Truncate,
        unix_timestamp_offset: None,
    };
    if value.ends_with('z') || value.contains('−') {
        return Err(ClaimError::InvalidTimestamp);
    }
    let parsed = DateTime::parse_bytes_rfc3339_with_config(value.as_bytes(), &TIME)
        .map_err(|_| ClaimError::InvalidTimestamp)?;
    if parsed.date.year == 0 || parsed.time.tz_offset.is_none() {
        return Err(ClaimError::InvalidTimestamp);
    }
    let instant = event_time::as_stored_instant(parsed).ok_or(ClaimError::InvalidTimestamp)?;
    Timestamp::from_microsecond(instant.unix_microseconds())
        .map_err(|_| ClaimError::InvalidTimestamp)
}

fn sleep_label(tool: &str, path: &[Value], language: &str) -> Option<&'static str> {
    if tool != "analysis_sleep"
        || path.len() != 4
        || path[0] != "rows"
        || !path[1].is_u64()
        || path[2] != "main_interval"
    {
        return None;
    }
    let key = match path[3].as_str()? {
        "start" => "evidence.sleep.start",
        "end" => "evidence.sleep.end",
        _ => return None,
    };
    catalog_text(language, key)
}

fn cited_value<'a, 'claim>(
    claim: &'claim Value,
    evidence: &[AnswerEvidence<'a>],
    selected: &BTreeSet<u64>,
) -> Result<(AnswerEvidence<'a>, &'claim [Value], &'a Value), ClaimError> {
    let fields = claim
        .as_object()
        .filter(|fields| fields.len() == 3)
        .ok_or(ClaimError::InvalidClaim)?;
    let id = fields
        .get("evidence_id")
        .and_then(Value::as_u64)
        .filter(|id| *id > 0)
        .ok_or(ClaimError::InvalidClaim)?;
    let path = fields
        .get("path")
        .and_then(Value::as_array)
        .filter(|path| (1..=12).contains(&path.len()))
        .ok_or(ClaimError::InvalidClaim)?;
    if !selected.contains(&id) {
        return Err(ClaimError::MissingEvidence);
    }
    let item = evidence
        .iter()
        .rev()
        .find(|item| item.id == id && item.result.get("error").is_none())
        .copied()
        .ok_or(ClaimError::MissingEvidence)?;
    let mut value = item.result;
    for segment in path {
        value = if let Some(key) = segment.as_str() {
            if !(1..=120).contains(&key.chars().count()) {
                return Err(ClaimError::InvalidClaim);
            }
            value.as_object().and_then(|object| object.get(key))
        } else if let Some(index) = segment.as_u64().filter(|index| *index <= 10_000) {
            let index = usize::try_from(index).map_err(|_| ClaimError::InvalidClaim)?;
            value.as_array().and_then(|items| items.get(index))
        } else {
            return Err(ClaimError::InvalidClaim);
        }
        .ok_or(ClaimError::MissingPath)?;
    }
    Ok((item, path, value))
}

fn evidence_path(path: &[Value]) -> Result<String, ClaimError> {
    let mut path_text = String::new();
    for segment in path {
        let key = match segment.as_str() {
            Some(key) => key.replace('~', "~0").replace('/', "~1"),
            None => segment
                .as_u64()
                .ok_or(ClaimError::InvalidClaim)?
                .to_string(),
        };
        path_text.push('/');
        path_text.push_str(&key);
    }
    Ok(path_text)
}

enum ObservedNumber {
    Integer(BigInt),
    Float(f64),
}

impl ObservedNumber {
    fn from_json(number: &Number) -> Option<Self> {
        let value = number.as_str();
        if value.contains(['.', 'e', 'E']) {
            let value = value
                .parse::<f64>()
                .ok()
                .filter(|value| value.is_finite())?;
            Some(Self::Float(value))
        } else {
            value.parse().ok().map(Self::Integer)
        }
    }

    #[expect(
        clippy::float_cmp,
        reason = "Numeric citations must match the observed value exactly"
    )]
    fn matches(&self, proposed: &Self) -> bool {
        match (self, proposed) {
            (Self::Integer(left), Self::Integer(right)) => left == right,
            (Self::Float(left), Self::Float(right)) => left == right,
            (Self::Integer(integer), Self::Float(float))
            | (Self::Float(float), Self::Integer(integer)) => {
                float.fract() == 0.0 && float.to_bigint().as_ref() == Some(integer)
            }
        }
    }

    fn render(&self) -> String {
        match self {
            Self::Integer(value) => value.to_string(),
            Self::Float(value) => {
                let text = format!("{value:?}");
                match text.split_once('e') {
                    Some((mantissa, exponent)) => exponent
                        .parse::<i16>()
                        .map_or_else(|_| text.clone(), |exp| format!("{mantissa}e{exp:+03}")),
                    None => text,
                }
            }
        }
    }
}
