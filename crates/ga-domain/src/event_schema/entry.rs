use std::{error::Error, fmt};

use serde_json::{Map, Value};

use super::{InvalidEntryValues, custom::field_name, validate_entry_values};
use crate::{
    event_input::known_timezone,
    event_payload::finite_number,
    event_provenance::{
        EventProvenance, EventTopology, InvalidClockUncertainty, derive_provenance,
    },
    event_replay::{ReplayConflict, audited_instant, same_json},
    timestamp::StoredInstant,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CustomEntryError {
    Shape,
    Key,
    Timestamp,
    Timezone,
    Source,
    Status,
    Value(InvalidEntryValues),
    Unit,
    Topology,
}

impl fmt::Display for CustomEntryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Shape => "Custom entry fields are invalid",
            Self::Key => "Custom entry definition key is invalid",
            Self::Timestamp => "Custom entry timestamps are invalid",
            Self::Timezone => "Custom entry timezone is unknown",
            Self::Source => "Custom entry source is invalid",
            Self::Status => "Custom entry status is invalid",
            Self::Value(error) => return error.fmt(f),
            Self::Unit => "Custom entry units must match the recorded definition",
            Self::Topology => "Custom entry time bounds do not match its definition",
        })
    }
}
impl Error for CustomEntryError {}

pub struct CustomEntry {
    key: String,
    start: StoredInstant,
    end: Option<StoredInstant>,
    display_times: [Option<String>; 2],
    timezone: Option<String>,
    source: &'static str,
    status: &'static str,
    confidence: f64,
    original_text: Option<String>,
    values: Value,
    units: Map<String, Value>,
}

pub struct ResolvedCustomEntry<'a> {
    pub key: &'a str,
    pub start: StoredInstant,
    pub end: Option<StoredInstant>,
    pub topology: EventTopology,
    pub timezone: &'a str,
    pub source: &'static str,
    pub status: &'static str,
    pub confidence: f64,
    pub original_text: Option<&'a str>,
    pub payload: Value,
}

impl CustomEntry {
    pub fn from_json(input: &[u8], max_bytes: usize) -> Result<Self, CustomEntryError> {
        if input.len() > max_bytes {
            return Err(CustomEntryError::Shape);
        }
        let value = serde_json::from_slice::<Value>(input).map_err(|_| CustomEntryError::Shape)?;
        Self::from_value(value)
    }

    pub fn from_value(value: Value) -> Result<Self, CustomEntryError> {
        let Value::Object(mut fields) = value else {
            return Err(CustomEntryError::Shape);
        };
        let key = fields
            .remove("definition_key")
            .and_then(|value| {
                value
                    .as_str()
                    .filter(|key| key.strip_prefix("user.").is_some_and(field_name))
                    .map(str::to_owned)
            })
            .ok_or(CustomEntryError::Key)?;
        let (start, display_start) = fields
            .remove("start")
            .as_ref()
            .and_then(StoredInstant::legacy_input_with_json)
            .ok_or(CustomEntryError::Timestamp)?;
        let (end, display_end) = match fields.remove("end") {
            None | Some(Value::Null) => (None, None),
            Some(value) => {
                let (end, display) = StoredInstant::legacy_input_with_json(&value)
                    .filter(|(end, _)| *end >= start)
                    .ok_or(CustomEntryError::Timestamp)?;
                (Some(end), Some(display))
            }
        };
        let timezone = match fields.remove("timezone") {
            None | Some(Value::Null) => None,
            Some(Value::String(zone)) if known_timezone(&zone) => Some(zone),
            _ => return Err(CustomEntryError::Timezone),
        };
        let source = match fields
            .remove("source")
            .as_ref()
            .map_or(Some("manual"), Value::as_str)
        {
            Some("manual") => "manual",
            Some("telegram_text") => "telegram_text",
            Some("telegram_button") => "telegram_button",
            Some("telegram_voice") => "telegram_voice",
            Some("mcp") => "mcp",
            Some("file_import") => "file_import",
            _ => return Err(CustomEntryError::Source),
        };
        let status = match fields
            .remove("status")
            .as_ref()
            .map_or(Some("confirmed"), Value::as_str)
        {
            Some("confirmed") => "confirmed",
            Some("needs_confirmation") => "needs_confirmation",
            _ => return Err(CustomEntryError::Status),
        };
        let confidence = match fields.remove("confidence") {
            None => 1.0,
            Some(value) => finite_number(0.0, 1.0, true)(value)
                .and_then(|value| value.as_f64())
                .ok_or(CustomEntryError::Shape)?,
        };
        let original_text = match fields.remove("original_text") {
            None | Some(Value::Null) => None,
            Some(Value::String(text)) if text.chars().count() <= 16_000 => Some(text),
            _ => return Err(CustomEntryError::Shape),
        };
        let values = fields
            .remove("values")
            .filter(|value| {
                value
                    .as_object()
                    .is_some_and(|fields| fields.len() <= 32 && !fields.contains_key("type"))
            })
            .ok_or(CustomEntryError::Shape)?;
        let units = match fields.remove("units") {
            None => Map::new(),
            Some(Value::Object(fields))
                if fields.len() <= 32 && fields.values().all(Value::is_string) =>
            {
                fields
            }
            _ => return Err(CustomEntryError::Unit),
        };
        if !fields.is_empty() {
            return Err(CustomEntryError::Shape);
        }
        Ok(Self {
            key,
            start,
            end,
            display_times: [Some(display_start), display_end],
            timezone,
            source,
            status,
            confidence,
            original_text,
            values,
            units,
        })
    }

    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }

    /// Attribute a validated local MCP entry to the authenticated transport.
    #[must_use]
    pub fn into_mcp_entry(mut self) -> Self {
        self.source = "mcp";
        self
    }

    #[must_use]
    pub fn display_times(&self) -> [Option<&str>; 2] {
        self.display_times.each_ref().map(|time| time.as_deref())
    }

    pub fn resolve<'a>(
        &'a self,
        schema: &Value,
        metadata: &Value,
        topology: &str,
        fallback_timezone: &'a str,
    ) -> Result<ResolvedCustomEntry<'a>, CustomEntryError> {
        let mut end = self.end;
        let positive = end.is_some_and(|end| end > self.start);
        let topology = match topology {
            "point" if !positive => EventTopology::Point,
            "open_interval" if end.is_none() => EventTopology::OpenInterval,
            "bounded_interval" | "open_interval" | "flexible" if positive => {
                EventTopology::BoundedInterval
            }
            "open_interval" | "flexible" => EventTopology::Point,
            _ => return Err(CustomEntryError::Topology),
        };
        if topology == EventTopology::Point {
            end = None;
        }
        let timezone = self.timezone.as_deref().unwrap_or(fallback_timezone);
        if !known_timezone(timezone) {
            return Err(CustomEntryError::Timezone);
        }
        validate_entry_values(schema, &self.values).map_err(CustomEntryError::Value)?;
        if self.units.iter().any(|(field, unit)| {
            metadata.get(field).and_then(|field| field.get("unit")) != Some(unit)
        }) {
            return Err(CustomEntryError::Unit);
        }
        let mut payload = self.values.clone();
        payload["type"] = Value::String(self.key.clone());
        Ok(ResolvedCustomEntry {
            key: &self.key,
            start: self.start,
            end,
            topology,
            timezone,
            source: self.source,
            status: self.status,
            confidence: self.confidence,
            original_text: self.original_text.as_deref(),
            payload,
        })
    }
}

impl ResolvedCustomEntry<'_> {
    pub fn provenance(
        &self,
        actor: &str,
        at: StoredInstant,
    ) -> Result<EventProvenance, InvalidClockUncertainty> {
        derive_provenance(
            Some(actor),
            self.source,
            self.status,
            self.topology,
            at,
            None,
        )
    }

    pub fn verify_creation(
        &self,
        version: uuid::Uuid,
        record: &Value,
    ) -> Result<(), ReplayConflict> {
        let audit = record.as_object().ok_or(ReplayConflict::AuditUnavailable)?;
        let get = |key| audit.get(key).ok_or(ReplayConflict::AuditUnavailable);
        if audited_instant(get("start")?)? != Some(self.start)
            || audited_instant(get("end")?)? != self.end
        {
            return Err(ReplayConflict::InputChanged);
        }
        if get("kind")? != self.key
            || get("definition_version_id")?
                .as_str()
                .and_then(|id| uuid::Uuid::parse_str(id).ok())
                != Some(version)
            || get("timezone")? != self.timezone
            || get("topology")? != self.topology.label()
            || get("source")? != self.source
            || get("status")? != self.status
            || get("confidence")?.as_f64() != Some(self.confidence)
            || get("original_text")? != &self.original_text.map_or(Value::Null, Value::from)
            || !same_json(get("payload")?, &self.payload)
        {
            return Err(ReplayConflict::InputChanged);
        }
        Ok(())
    }
}
