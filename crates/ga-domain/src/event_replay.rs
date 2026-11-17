use std::{error::Error, fmt};

use serde_json::Value;

use crate::{event_input::EventInput, event_payload::SystemPayload, timestamp::StoredInstant};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplayConflict {
    InvalidKey,
    AuditUnavailable,
    InputChanged,
}

impl fmt::Display for ReplayConflict {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidKey => "Invalid idempotency key",
            Self::AuditUnavailable => "Creation audit unavailable for idempotent replay",
            Self::InputChanged => "Idempotency key already used for different data",
        })
    }
}
impl Error for ReplayConflict {}

pub fn check_idempotency_key(key: &str) -> Result<(), ReplayConflict> {
    if key.is_empty() || key.chars().count() > 200 {
        return Err(ReplayConflict::InvalidKey);
    }
    Ok(())
}

impl EventInput {
    /// Compare a retry with the immutable creation audit. Later edits to the
    /// current revision are not part of the original request.
    pub fn verify_creation_replay(
        &self,
        creation_after: Option<&Value>,
    ) -> Result<(), ReplayConflict> {
        let audit = creation_after
            .and_then(Value::as_object)
            .ok_or(ReplayConflict::AuditUnavailable)?;
        let required = |field| audit.get(field).ok_or(ReplayConflict::AuditUnavailable);
        let start = audited_instant(required("start")?)?.ok_or(ReplayConflict::AuditUnavailable)?;
        let end = audited_instant(required("end")?)?;
        let text = match required("original_text")? {
            Value::Null => None,
            Value::String(text) => Some(text.as_str()),
            _ => return Err(ReplayConflict::AuditUnavailable),
        };
        let recorded = required("payload")?;
        let normalized;
        let payload = if self.payload().kind() == "caffeine" && recorded["type"] == "caffeine" {
            normalized = SystemPayload::from_value(recorded.clone())
                .map_err(|_| ReplayConflict::AuditUnavailable)?;
            normalized.json()
        } else {
            recorded
        };
        if start != self.start()
            || end != self.end()
            || required("timezone")?.as_str() != Some(self.timezone())
            || required("source")?.as_str() != Some(self.source().label())
            || required("status")?.as_str() != Some(self.status().label())
            || numeric(required("confidence")?) != Some(self.confidence())
            || text != self.original_text()
            || required("kind")?.as_str() != Some(self.payload().kind())
            || !same_json(payload, self.payload().json())
        {
            return Err(ReplayConflict::InputChanged);
        }
        Ok(())
    }
}

fn audited_instant(value: &Value) -> Result<Option<StoredInstant>, ReplayConflict> {
    match value {
        Value::Null => Ok(None),
        Value::String(value) => value
            .parse()
            .map(Some)
            .map_err(|_| ReplayConflict::AuditUnavailable),
        _ => Err(ReplayConflict::AuditUnavailable),
    }
}

fn same_json(left: &Value, right: &Value) -> bool {
    if left == right {
        return true;
    }
    match (left, right) {
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| same_json(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .all(|(key, left)| right.get(key).is_some_and(|right| same_json(left, right)))
        }
        (Value::Number(_) | Value::Bool(_), Value::Number(_) | Value::Bool(_)) => {
            match (integral(left), integral(right)) {
                (Some(left), Some(right)) => left == right,
                (Some(integer), None) => same_mixed(integer, right),
                (None, Some(integer)) => same_mixed(integer, left),
                (None, None) => numeric(left)
                    .zip(numeric(right))
                    .is_some_and(|(left, right)| left == right),
            }
        }
        _ => false,
    }
}

fn integral(value: &Value) -> Option<i128> {
    match value {
        Value::Bool(value) => Some(i128::from(*value)),
        Value::Number(number) => number.as_i128().or_else(|| number.as_u64().map(i128::from)),
        _ => None,
    }
}

fn numeric(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(value) => Some(if *value { 1.0 } else { 0.0 }),
        Value::Number(number) => number.as_f64(),
        _ => None,
    }
}

fn same_mixed(integer: i128, value: &Value) -> bool {
    numeric(value).is_some_and(|value| {
        value.trunc() == value
            && value >= (i128::MIN as f64)
            && value < (i128::MAX as f64)
            && value as i128 == integer
    })
}
