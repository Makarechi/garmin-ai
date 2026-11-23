mod calendar;
mod clock;
mod context;
mod patterns;
mod submission;
#[cfg(test)]
mod tests;
mod unicode;
mod values;
pub use submission::ValidatedTrackerSubmission;

use std::{error::Error, fmt, sync::OnceLock};

use jsonschema::Validator;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{
    event_payload::finite_number, event_schema::TrackerSetupDraft,
    model_request::ApprovedModelSchema, model_response::application_validator,
    text_offsets::from_code_points, timestamp::StoredInstant,
};

#[derive(Debug)]
pub enum ExtractionError {
    Invalid,
    Contract,
    Field,
    Fact,
    Temporal {
        field: &'static str,
        evidence: Value,
    },
}
impl fmt::Display for ExtractionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Invalid => "Invalid tracker extraction",
            Self::Contract => "Tracker extraction contract unavailable",
            Self::Field => "Extracted value is not supported by its evidence",
            Self::Fact => "Source context does not establish a recorded fact",
            Self::Temporal { .. } => "Extracted time is not supported by its evidence",
        })
    }
}
impl Error for ExtractionError {}
impl From<fancy_regex::Error> for ExtractionError {
    fn from(_: fancy_regex::Error) -> Self {
        Self::Contract
    }
}

pub struct TrackerExtraction {
    document: Value,
}

impl TrackerExtraction {
    pub fn from_value(raw: &Value) -> Result<Self, ExtractionError> {
        let mut document = raw.clone();
        let object = document.as_object_mut().ok_or(ExtractionError::Invalid)?;
        for name in ["definition_version_id", "event_id"] {
            if let Some(value) = object.get_mut(name).filter(|value| !value.is_null()) {
                let id = value.as_str().ok_or(ExtractionError::Invalid)?;
                if id.contains(':') && !id.starts_with("urn:uuid:") {
                    return Err(ExtractionError::Invalid);
                }
                *value = json!(
                    Uuid::parse_str(id)
                        .map_err(|_| ExtractionError::Invalid)?
                        .to_string()
                );
            }
        }
        for name in ["start", "end"] {
            if let Some(value) = object.get_mut(name).filter(|value| !value.is_null()) {
                let (_, time) =
                    StoredInstant::legacy_input_with_json(value).ok_or(ExtractionError::Invalid)?;
                *value = json!(time);
            }
        }
        let confidence = object
            .get("confidence")
            .ok_or(ExtractionError::Invalid)?
            .clone();
        object.insert(
            "confidence".to_owned(),
            finite_number(0.0, 1.0, true)(confidence).ok_or(ExtractionError::Invalid)?,
        );
        if let Some(draft) = object
            .get_mut("tracker_draft")
            .filter(|value| !value.is_null())
        {
            *draft = TrackerSetupDraft::from_json(draft)
                .map_err(|_| ExtractionError::Invalid)?
                .document()
                .clone();
        }
        for name in ["start_evidence", "end_evidence"] {
            if let Some(evidence) = object.get_mut(name).filter(|value| value.is_object()) {
                normalize_evidence(evidence)?;
            }
        }
        if let Some(fields) = object.get_mut("fields").and_then(Value::as_array_mut) {
            for field in fields {
                if let Some(field) = field.as_object_mut() {
                    for name in ["evidence", "unit_evidence"] {
                        if let Some(evidence) =
                            field.get_mut(name).filter(|value| value.is_object())
                        {
                            normalize_evidence(evidence)?;
                        }
                    }
                    if let Some(value) = field.get_mut("value")
                        && let Some(number) = value.as_number()
                        && number.as_str().contains(['.', 'e', 'E'])
                    {
                        let number = number
                            .as_f64()
                            .filter(|value| value.is_finite())
                            .ok_or(ExtractionError::Invalid)?;
                        let mut bytes = Vec::new();
                        crate::legacy_json::write_float(&mut bytes, number)
                            .map_err(|_| ExtractionError::Invalid)?;
                        *value =
                            serde_json::from_slice(&bytes).map_err(|_| ExtractionError::Invalid)?;
                    }
                    field.entry("unit").or_insert(Value::Null);
                    field.entry("unit_evidence").or_insert(Value::Null);
                }
            }
        }
        if !contract()?.is_valid(&document) {
            return Err(ExtractionError::Invalid);
        }
        let object = document.as_object_mut().ok_or(ExtractionError::Invalid)?;
        for name in [
            "definition_version_id",
            "event_id",
            "start",
            "end",
            "start_evidence",
            "end_evidence",
            "tracker_draft",
            "clarification",
        ] {
            object.entry(name).or_insert(Value::Null);
        }
        object.entry("fields").or_insert_with(|| json!([]));
        let intent = object["intent"].as_str().ok_or(ExtractionError::Invalid)?;
        let present = |key| object.get(key).is_some_and(|value| !value.is_null());
        if matches!(intent, "propose_tracker" | "change_tracker") && !present("tracker_draft")
            || intent == "change_tracker" && !present("definition_version_id")
            || intent == "create_entry"
                && !(present("definition_version_id")
                    && present("start")
                    && present("start_evidence"))
            || intent == "update_entry"
                && !(present("event_id") && present("definition_version_id"))
        {
            return Err(ExtractionError::Invalid);
        }
        Ok(Self { document })
    }

    pub fn document(&self) -> &Value {
        &self.document
    }
}

fn contract() -> Result<&'static Validator, ExtractionError> {
    static CONTRACT: OnceLock<Option<Validator>> = OnceLock::new();
    CONTRACT
        .get_or_init(|| {
            let document = ApprovedModelSchema::TrackerExtraction.document().ok()?;
            let schema = serde_json::from_slice(document).ok()?;
            application_validator(&schema).ok()
        })
        .as_ref()
        .ok_or(ExtractionError::Contract)
}

fn verified_quote<'a>(
    source: &'a str,
    evidence: &Value,
) -> Result<(&'a str, usize), ExtractionError> {
    let start = evidence["start"]
        .as_u64()
        .and_then(|value| usize::try_from(value).ok())
        .ok_or(ExtractionError::Invalid)?;
    let end = evidence["end"]
        .as_u64()
        .and_then(|value| usize::try_from(value).ok())
        .ok_or(ExtractionError::Invalid)?;
    if start >= end {
        return Err(ExtractionError::Invalid);
    }
    let range = from_code_points(source, start..end)
        .ok_or(ExtractionError::Invalid)?
        .utf8;
    let offset = range.start;
    let quote = &source[range];
    if Some(quote) != evidence["quote"].as_str() {
        return Err(ExtractionError::Invalid);
    }
    Ok((quote, offset))
}

fn normalize_evidence(value: &mut Value) -> Result<(), ExtractionError> {
    for name in ["start", "end"] {
        if let Some(value) = value.get_mut(name) {
            let integer =
                crate::legacy_json::integer_argument(value).ok_or(ExtractionError::Invalid)?;
            *value =
                serde_json::from_str(&integer.to_string()).map_err(|_| ExtractionError::Invalid)?;
        }
    }
    Ok(())
}
