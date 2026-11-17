use std::{error::Error, fmt};

use jiter::JsonValue;
use serde_json::Value;

use crate::{
    event_payload::{InvalidPayload, SystemPayload, finite_number, json_value},
    event_time,
    timestamp::StoredInstant,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EventSource {
    Manual,
    TelegramText,
    TelegramButton,
    TelegramVoice,
    Mcp,
    Inferred,
    Wearable,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EventStatus {
    Confirmed,
    Inferred,
    NeedsConfirmation,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ValidationMode {
    NewReport,
    AuditedSnapshot,
}

pub struct EventInput {
    start: StoredInstant,
    end: Option<StoredInstant>,
    timezone: String,
    source: EventSource,
    status: EventStatus,
    confidence: f64,
    original_text: Option<String>,
    payload: SystemPayload,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvalidEvent {
    InputTooLarge,
    InvalidObject,
    InvalidField(&'static str),
    UnknownField,
    Payload(InvalidPayload),
    UnknownTimezone,
    MedicationNotReported,
    PointObservationRequired,
    CoverageRequired,
    ReversedInterval,
    ExplicitReportRequired,
    InferredCannotBeConfirmed,
}

impl fmt::Display for InvalidEvent {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InputTooLarge => "Diary report exceeds the byte limit",
            Self::InvalidObject => "Diary report must be a JSON object",
            Self::InvalidField(field) => {
                return write!(formatter, "Invalid diary report field {field}");
            }
            Self::UnknownField => "Diary report contains an unknown field",
            Self::Payload(error) => return error.fmt(formatter),
            Self::UnknownTimezone => "Diary report timezone is unknown",
            Self::MedicationNotReported => {
                "Incomplete medication requires a confirmed reported intake"
            }
            Self::PointObservationRequired => "Diary observation requires a single instant",
            Self::CoverageRequired => "Diary observation requires a nonempty covered interval",
            Self::ReversedInterval => "Diary report end must not precede its start",
            Self::ExplicitReportRequired => "Diary observation requires an explicit report",
            Self::InferredCannotBeConfirmed => {
                "Inferred data requires user action before confirmation"
            }
        })
    }
}
impl Error for InvalidEvent {}

impl EventInput {
    pub fn from_json(
        input: &[u8],
        max_bytes: usize,
        mode: ValidationMode,
    ) -> Result<Self, InvalidEvent> {
        if input.len() > max_bytes {
            return Err(InvalidEvent::InputTooLarge);
        }
        let parsed = JsonValue::parse(input, true).map_err(|_| InvalidEvent::InvalidObject)?;
        let Some(Value::Object(mut fields)) = json_value(&parsed) else {
            return Err(InvalidEvent::InvalidObject);
        };
        let start = fields
            .remove("start")
            .as_ref()
            .and_then(event_time::aware)
            .ok_or(InvalidEvent::InvalidField("start"))?;
        let end = match fields.remove("end") {
            None | Some(Value::Null) => None,
            Some(value) => {
                Some(event_time::aware(&value).ok_or(InvalidEvent::InvalidField("end"))?)
            }
        };
        let timezone = match fields.remove("timezone") {
            None => "Europe/Bratislava".to_owned(),
            Some(Value::String(name)) => name,
            _ => return Err(InvalidEvent::InvalidField("timezone")),
        };
        if !known_timezone(&timezone) {
            return Err(InvalidEvent::UnknownTimezone);
        }
        let source = EventSource::parse(fields.remove("source"))?;
        let status = EventStatus::parse(fields.remove("status"))?;
        let confidence = match fields.remove("confidence") {
            None => 1.0,
            Some(value) => finite_number(0.0, 1.0, true)(value)
                .and_then(|number| number.as_f64())
                .ok_or(InvalidEvent::InvalidField("confidence"))?,
        };
        let original_text = match fields.remove("original_text") {
            None | Some(Value::Null) => None,
            Some(Value::String(text)) if text.chars().count() <= 16_000 => Some(text),
            _ => return Err(InvalidEvent::InvalidField("original_text")),
        };
        let payload = fields
            .remove("payload")
            .ok_or(InvalidEvent::InvalidField("payload"))?;
        if !fields.is_empty() {
            return Err(InvalidEvent::UnknownField);
        }
        let payload = SystemPayload::from_value(payload).map_err(InvalidEvent::Payload)?;
        let mut event = Self {
            start,
            end,
            timezone,
            source,
            status,
            confidence,
            original_text,
            payload,
        };
        event.verify_context(mode)?;
        Ok(event)
    }

    fn verify_context(&mut self, mode: ValidationMode) -> Result<(), InvalidEvent> {
        let kind = self.payload.kind();
        let machine_source = matches!(self.source, EventSource::Inferred | EventSource::Wearable);
        if kind == "medication"
            && ["name", "dose", "unit"]
                .iter()
                .any(|field| self.payload.json()[field].is_null())
            && (machine_source || self.status != EventStatus::Confirmed)
        {
            return Err(InvalidEvent::MedicationNotReported);
        }
        let strict_point = matches!(kind, "wellbeing_observation" | "activity_effort");
        if strict_point || kind == "symptom_observation" {
            if self.end.is_some_and(|end| end != self.start) {
                return Err(InvalidEvent::PointObservationRequired);
            }
            if strict_point {
                self.end = None;
            }
        }
        if matches!(
            kind,
            "caffeine_absence" | "headache_observation" | "caffeine_log_complete"
        ) && self.end.is_none_or(|end| end <= self.start)
        {
            return Err(InvalidEvent::CoverageRequired);
        }
        if self.end.is_some_and(|end| end < self.start) {
            return Err(InvalidEvent::ReversedInterval);
        }
        if mode == ValidationMode::NewReport {
            self.validate_for_write()?;
        }
        if self.source == EventSource::Inferred && self.status == EventStatus::Confirmed {
            return Err(InvalidEvent::InferredCannotBeConfirmed);
        }
        Ok(())
    }

    /// Recheck rules reserved for newly submitted reports after loading an audit.
    pub fn validate_for_write(&self) -> Result<(), InvalidEvent> {
        if matches!(
            self.payload.kind(),
            "wellbeing_observation" | "activity_effort"
        ) && (matches!(self.source, EventSource::Inferred | EventSource::Wearable)
            || self.status == EventStatus::Inferred)
        {
            return Err(InvalidEvent::ExplicitReportRequired);
        }
        Ok(())
    }

    /// Apply authenticated message evidence to a new diary report.
    pub fn for_agent_report(
        &self,
        source: EventSource,
        original_text: &str,
        linked_parent: Option<uuid::Uuid>,
    ) -> Result<Self, InvalidEvent> {
        if !matches!(
            source,
            EventSource::Manual
                | EventSource::TelegramText
                | EventSource::TelegramButton
                | EventSource::TelegramVoice
        ) {
            return Err(InvalidEvent::InvalidField("source"));
        }
        if original_text.chars().count() > 16_000 {
            return Err(InvalidEvent::InvalidField("original_text"));
        }
        let mut payload = self.payload.json().clone();
        if let Some(parent) = linked_parent {
            if self.payload.kind() != "medication" || !payload["reason_event_id"].is_null() {
                return Err(InvalidEvent::InvalidField("reason_event_id"));
            }
            payload["reason_event_id"] = Value::String(parent.to_string());
        }
        let mut event = Self {
            start: self.start,
            end: self.end,
            timezone: self.timezone.clone(),
            source,
            status: EventStatus::Confirmed,
            confidence: self.confidence,
            original_text: Some(original_text.to_owned()),
            payload: SystemPayload::from_value(payload).map_err(InvalidEvent::Payload)?,
        };
        event.verify_context(ValidationMode::NewReport)?;
        Ok(event)
    }

    pub fn start(&self) -> StoredInstant {
        self.start
    }

    pub fn source_calendar_date(&self) -> Option<time::Date> {
        source_calendar_date(self.start, &self.timezone)
    }
    pub fn end(&self) -> Option<StoredInstant> {
        self.end
    }
    pub fn timezone(&self) -> &str {
        &self.timezone
    }
    pub fn source(&self) -> EventSource {
        self.source
    }
    pub fn status(&self) -> EventStatus {
        self.status
    }
    pub fn confidence(&self) -> f64 {
        self.confidence
    }
    pub fn original_text(&self) -> Option<&str> {
        self.original_text.as_deref()
    }
    pub fn payload(&self) -> &SystemPayload {
        &self.payload
    }
}

impl EventSource {
    fn parse(value: Option<Value>) -> Result<Self, InvalidEvent> {
        let label = match &value {
            None => "manual",
            Some(Value::String(label)) => label,
            _ => return Err(InvalidEvent::InvalidField("source")),
        };
        match label {
            "manual" => Ok(Self::Manual),
            "telegram_text" => Ok(Self::TelegramText),
            "telegram_button" => Ok(Self::TelegramButton),
            "telegram_voice" => Ok(Self::TelegramVoice),
            "mcp" => Ok(Self::Mcp),
            "inferred" => Ok(Self::Inferred),
            "wearable" => Ok(Self::Wearable),
            _ => Err(InvalidEvent::InvalidField("source")),
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Manual => "manual",
            Self::TelegramText => "telegram_text",
            Self::TelegramButton => "telegram_button",
            Self::TelegramVoice => "telegram_voice",
            Self::Mcp => "mcp",
            Self::Inferred => "inferred",
            Self::Wearable => "wearable",
        }
    }
}

impl EventStatus {
    fn parse(value: Option<Value>) -> Result<Self, InvalidEvent> {
        let label = match &value {
            None => "confirmed",
            Some(Value::String(label)) => label,
            _ => return Err(InvalidEvent::InvalidField("status")),
        };
        match label {
            "confirmed" => Ok(Self::Confirmed),
            "inferred" => Ok(Self::Inferred),
            "needs_confirmation" => Ok(Self::NeedsConfirmation),
            _ => Err(InvalidEvent::InvalidField("status")),
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Confirmed => "confirmed",
            Self::Inferred => "inferred",
            Self::NeedsConfirmation => "needs_confirmation",
        }
    }
}

pub fn known_timezone(name: &str) -> bool {
    let database = jiff::tz::db();
    database.get(name).is_ok_and(|zone| {
        zone.iana_name() == Some(name)
            || database
                .available()
                .any(|available| available.as_str() == name)
    })
}

pub fn source_calendar_date(start: StoredInstant, timezone: &str) -> Option<time::Date> {
    source_calendar_clock(start, timezone).map(|(date, _)| date)
}

pub fn source_calendar_clock(start: StoredInstant, timezone: &str) -> Option<(time::Date, u8)> {
    let instant = jiff::Timestamp::from_microsecond(start.unix_microseconds()).ok()?;
    let zone = jiff::tz::db().get(timezone).ok()?;
    let local = instant.to_zoned_datetime(zone);
    let date = time::Date::from_calendar_date(
        i32::from(local.year()),
        time::Month::try_from(u8::try_from(local.month()).ok()?).ok()?,
        u8::try_from(local.day()).ok()?,
    )
    .ok()?;
    Some((date, u8::try_from(local.hour()).ok()?))
}

/// Interpret local midnight using the first offset at overlaps, as Python ZoneInfo does.
pub fn local_midnight(day: time::Date, timezone: &str) -> Option<StoredInstant> {
    let zone = jiff::tz::db().get(timezone).ok()?;
    let day = jiff::civil::Date::new(
        i16::try_from(day.year()).ok()?,
        u8::from(day.month()) as i8,
        day.day() as i8,
    )
    .ok()?;
    let midnight = zone
        .to_ambiguous_timestamp(day.at(0, 0, 0, 0))
        .compatible()
        .ok()?;
    StoredInstant::from_unix_microseconds(midnight.as_microsecond()).ok()
}
