//! Untrusted offline marks and their Python-compatible immutable receipt identity.

use std::{error::Error, fmt};

use serde_json::{Value, json};
use uuid::Uuid;

use crate::{
    event_input::{EventInput, ValidationMode},
    legacy_json::{Utf8Layout, encode_ascii},
    sha256_hex,
    timestamp::StoredInstant,
};

pub const MAX_BATCH_BYTES: usize = 1024 * 1024;
const MAX_CLOCK_UNCERTAINTY: u64 = 31_536_000;
const DEVICE_NOTICE: &str = "Отметка с часов: время и содержание требуют подтверждения владельца.";

pub struct WearableMark {
    id: Uuid,
    device_time: String,
    clock_uncertainty_seconds: Option<u32>,
    digest: String,
    report: EventInput,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidWearableBatch;

impl fmt::Display for InvalidWearableBatch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Wearable marks are invalid")
    }
}

impl Error for InvalidWearableBatch {}

pub fn parse_batch(input: &[u8]) -> Result<Vec<WearableMark>, InvalidWearableBatch> {
    if input.len() > MAX_BATCH_BYTES {
        return Err(InvalidWearableBatch);
    }
    let Value::Object(mut body) =
        serde_json::from_slice(input).map_err(|_| InvalidWearableBatch)?
    else {
        return Err(InvalidWearableBatch);
    };
    let Some(Value::Array(marks)) = body.remove("marks") else {
        return Err(InvalidWearableBatch);
    };
    if !body.is_empty() || !(1..=20).contains(&marks.len()) {
        return Err(InvalidWearableBatch);
    }
    marks.into_iter().map(WearableMark::parse).collect()
}

impl WearableMark {
    fn parse(value: Value) -> Result<Self, InvalidWearableBatch> {
        let Value::Object(mut fields) = value else {
            return Err(InvalidWearableBatch);
        };
        let id = fields
            .remove("id")
            .as_ref()
            .and_then(Value::as_str)
            .and_then(|id| Uuid::parse_str(id).ok())
            .ok_or(InvalidWearableBatch)?;
        let input_time = fields.remove("device_time").ok_or(InvalidWearableBatch)?;
        let (_, canonical_time) =
            StoredInstant::legacy_input_with_json(&input_time).ok_or(InvalidWearableBatch)?;
        let timezone = fields
            .remove("timezone")
            .filter(|zone| {
                zone.as_str()
                    .is_some_and(|zone| (1..=100).contains(&zone.chars().count()))
            })
            .ok_or(InvalidWearableBatch)?;
        let uncertainty = match fields.remove("clock_uncertainty_seconds") {
            None | Some(Value::Null) => None,
            Some(value) => Some(
                value
                    .as_u64()
                    .filter(|value| *value <= MAX_CLOCK_UNCERTAINTY)
                    .and_then(|value| u32::try_from(value).ok())
                    .ok_or(InvalidWearableBatch)?,
            ),
        };
        let payload = fields.remove("payload").ok_or(InvalidWearableBatch)?;
        if !fields.is_empty()
            || !matches!(payload["type"].as_str(), Some("caffeine" | "medication"))
            || !payload["reason_event_id"].is_null()
        {
            return Err(InvalidWearableBatch);
        }
        let input = json!({
            "start": input_time, "timezone": timezone,
            "source": "wearable", "status": "needs_confirmation", "confidence": 0,
            "original_text": DEVICE_NOTICE, "payload": payload,
        });
        let bytes = serde_json::to_vec(&input).map_err(|_| InvalidWearableBatch)?;
        let report = EventInput::from_json(&bytes, MAX_BATCH_BYTES, ValidationMode::NewReport)
            .map_err(|_| InvalidWearableBatch)?;
        let normalized = json!({
            "id": id.to_string(), "device_time": canonical_time, "timezone": report.timezone(),
            "clock_uncertainty_seconds": uncertainty, "payload": report.payload().json(),
        });
        let bytes = serde_json::to_vec(&normalized).map_err(|_| InvalidWearableBatch)?;
        let ascii = encode_ascii(&bytes, Utf8Layout::Compact, MAX_BATCH_BYTES)
            .map_err(|_| InvalidWearableBatch)?;
        let device_time = match canonical_time.strip_suffix('Z') {
            Some(utc) => format!("{utc}+00:00"),
            None => canonical_time,
        };
        Ok(Self {
            id,
            device_time,
            clock_uncertainty_seconds: uncertainty,
            digest: sha256_hex(ascii.as_bytes()),
            report,
        })
    }

    #[must_use]
    pub fn id(&self) -> Uuid {
        self.id
    }

    #[must_use]
    pub fn device_time(&self) -> &str {
        &self.device_time
    }

    #[must_use]
    pub fn clock_uncertainty_seconds(&self) -> Option<u32> {
        self.clock_uncertainty_seconds
    }

    #[must_use]
    pub fn digest(&self) -> &str {
        &self.digest
    }

    #[must_use]
    pub fn report(&self) -> &EventInput {
        &self.report
    }
}
