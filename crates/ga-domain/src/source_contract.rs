use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fmt,
};

use jiter::JsonValue;
use serde_json::{Value, json};
use speedate::DateTime;

use crate::{
    event_input::known_timezone,
    event_payload::{boolean, numeric_text},
    event_time::{as_stored_instant, parse_aware_datetime},
    legacy_json::{Utf8Layout, write_value},
    sha256_hex,
    timestamp::StoredInstant,
};

const MAX_PAYLOAD_BYTES: usize = 16_384;
const MAX_COLLECTION_ITEMS: usize = 256;
const MAX_DEPTH: u8 = 8;
const MAX_RECORDS: usize = 1000;
const MAX_CURSOR_HISTORY: usize = 1000;
const MAX_ENCODED_RECORD: usize = 64_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceContractError {
    Shape,
    Capacity,
    Timestamp,
    Timezone,
    Payload,
    Deletion,
    DuplicateIdentity,
    Cursor,
    Instance,
    Interval,
    Window,
}

impl fmt::Display for SourceContractError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Shape => "Source record or page shape is invalid",
            Self::Capacity => "Source page exceeds its size limit",
            Self::Timestamp => "Source timestamp must have an explicit offset",
            Self::Timezone => "Source timezone must be an IANA name",
            Self::Payload => "Source payload exceeds its JSON limits",
            Self::Deletion => "Source deletion is invalid",
            Self::DuplicateIdentity => "Source page repeats a record identity",
            Self::Cursor => "Source cursor is invalid or repeats existing progress",
            Self::Instance => "Source page belongs to another instance",
            Self::Interval => "Source interval has invalid bounds",
            Self::Window => "Source record is outside the requested window",
        })
    }
}
impl Error for SourceContractError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceOperation {
    Upsert,
    Delete,
}

impl SourceOperation {
    pub fn label(self) -> &'static str {
        match self {
            Self::Upsert => "upsert",
            Self::Delete => "delete",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourcePageKind {
    Partial,
    CompleteIntervalSnapshot,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceTimeSemantics {
    Instant,
    Interval,
    CalendarDay,
}

#[derive(Clone, Copy, Debug)]
pub struct SourceTimestamp {
    original: DateTime,
    instant: StoredInstant,
}

impl SourceTimestamp {
    pub fn instant(self) -> StoredInstant {
        self.instant
    }

    /// The wire form preserves the supplied offset and six fractional digits when needed.
    pub fn model_json(self) -> String {
        self.original.to_string()
    }

    pub fn isoformat(self) -> String {
        let mut value = self.model_json();
        if value.ends_with('Z') {
            value.pop();
            value.push_str("+00:00");
        }
        value
    }
}

#[derive(Debug)]
pub struct SourceRecord {
    pub source_record_id: String,
    pub operation: SourceOperation,
    pub observed_at: SourceTimestamp,
    pub effective_at: SourceTimestamp,
    pub effective_end: Option<SourceTimestamp>,
    pub source_timezone: String,
    pub source_reference: String,
    payload: Vec<u8>,
}

impl SourceRecord {
    pub fn canonical_json(&self) -> Result<Vec<u8>, SourceContractError> {
        let payload =
            JsonValue::parse(&self.payload, false).map_err(|_| SourceContractError::Payload)?;
        let record = vec![
            (
                "effective_at".into(),
                JsonValue::Str(self.effective_at.model_json().into()),
            ),
            (
                "effective_end".into(),
                self.effective_end
                    .map_or(JsonValue::Null, |at| JsonValue::Str(at.model_json().into())),
            ),
            (
                "observed_at".into(),
                JsonValue::Str(self.observed_at.model_json().into()),
            ),
            (
                "operation".into(),
                JsonValue::Str(self.operation.label().into()),
            ),
            ("payload".into(), payload),
            (
                "source_record_id".into(),
                JsonValue::Str(self.source_record_id.as_str().into()),
            ),
            (
                "source_reference".into(),
                JsonValue::Str(self.source_reference.as_str().into()),
            ),
            (
                "source_timezone".into(),
                JsonValue::Str(self.source_timezone.as_str().into()),
            ),
        ];
        let mut encoded = Vec::with_capacity(self.payload.len() + 500);
        write_value(
            &mut encoded,
            &JsonValue::Object(record.into()),
            Utf8Layout::Compact,
        )
        .map_err(|_| SourceContractError::Payload)?;
        if encoded.len() > MAX_ENCODED_RECORD {
            return Err(SourceContractError::Capacity);
        }
        Ok(encoded)
    }
}

#[derive(Debug)]
pub struct SourcePage {
    pub instance_id: String,
    pub page_kind: SourcePageKind,
    pub fetched_at: SourceTimestamp,
    pub next_cursor: Option<String>,
    pub records: Vec<SourceRecord>,
    pub retry_after: Option<SourceTimestamp>,
}

#[derive(Clone, Copy, Debug)]
pub struct SourceCapabilities {
    pub observations: bool,
    pub corrections: bool,
    pub deletions: bool,
    pub cursor: bool,
    pub time_semantics: SourceTimeSemantics,
    pub max_page_size: usize,
}

impl SourceCapabilities {
    pub fn from_json(value: &Value) -> Result<Self, SourceContractError> {
        let fields = value.as_object().ok_or(SourceContractError::Shape)?;
        if fields.keys().any(|key| {
            !matches!(
                key.as_str(),
                "observations"
                    | "corrections"
                    | "deletions"
                    | "cursor"
                    | "time_semantics"
                    | "max_page_size"
            )
        }) {
            return Err(SourceContractError::Shape);
        }
        let flag = |key, default| {
            fields
                .get(key)
                .map_or(Some(default), |value| {
                    boolean(value.clone()).and_then(|value| value.as_bool())
                })
                .ok_or(SourceContractError::Shape)
        };
        let time_semantics = match fields
            .get("time_semantics")
            .map_or(Some("instant"), Value::as_str)
        {
            Some("instant") => SourceTimeSemantics::Instant,
            Some("interval") => SourceTimeSemantics::Interval,
            Some("calendar_day") => SourceTimeSemantics::CalendarDay,
            _ => return Err(SourceContractError::Shape),
        };
        let max_page_size = fields
            .get("max_page_size")
            .map_or(Some(100), page_size)
            .ok_or(SourceContractError::Capacity)?;
        Ok(Self {
            observations: flag("observations", true)?,
            corrections: flag("corrections", false)?,
            deletions: flag("deletions", false)?,
            cursor: flag("cursor", false)?,
            time_semantics,
            max_page_size,
        })
    }
}

fn page_size(value: &Value) -> Option<usize> {
    let valid = 1.0..=MAX_RECORDS as f64;
    let number = match value {
        Value::Bool(value) => f64::from(u8::from(*value)),
        Value::String(value) => {
            let value = numeric_text(value)?;
            let unsigned = value.strip_prefix(['+', '-']).unwrap_or(&value);
            let (whole, fraction) = unsigned.split_once('.').unwrap_or((unsigned, "0"));
            if whole.is_empty()
                || !whole.bytes().all(|byte| byte.is_ascii_digit())
                || fraction.is_empty()
                || !fraction.bytes().all(|byte| byte == b'0')
            {
                return None;
            }
            value.parse().ok()?
        }
        _ => value.as_f64()?,
    };
    (valid.contains(&number) && number.fract() == 0.0).then_some(number as usize)
}

pub struct SourceRequest<'a> {
    pub instance_id: &'a str,
    pub start: StoredInstant,
    pub end: StoredInstant,
    pub cursor: Option<&'a str>,
    pub previous_cursors: &'a BTreeSet<String>,
    pub capabilities: SourceCapabilities,
}

impl SourcePage {
    /// Validate provider JSON and preserve each normalized private raw record exactly.
    pub fn from_json(input: &[u8], max_records: usize) -> Result<Self, SourceContractError> {
        if max_records > MAX_RECORDS
            || input.len() > (max_records.saturating_add(1)) * MAX_ENCODED_RECORD
        {
            return Err(SourceContractError::Capacity);
        }
        let value = JsonValue::parse(input, true).map_err(|_| SourceContractError::Shape)?;
        let fields = object(&value)?;
        check_keys(
            &fields,
            &[
                "instance_id",
                "page_kind",
                "fetched_at",
                "next_cursor",
                "records",
                "retry_after",
            ],
        )?;
        let kind = match get(&fields, "page_kind").and_then(string) {
            Some("partial") => SourcePageKind::Partial,
            Some("complete_interval_snapshot") => SourcePageKind::CompleteIntervalSnapshot,
            _ => return Err(SourceContractError::Shape),
        };
        let next_cursor = optional(get(&fields, "next_cursor"))
            .map(|value| text(Some(value), 1000))
            .transpose()?;
        if kind == SourcePageKind::CompleteIntervalSnapshot && next_cursor.is_some() {
            return Err(SourceContractError::Cursor);
        }
        let raw = match get(&fields, "records") {
            None => &[][..],
            Some(JsonValue::Array(records)) if records.len() <= max_records => records.as_slice(),
            Some(JsonValue::Array(_)) => return Err(SourceContractError::Capacity),
            _ => return Err(SourceContractError::Shape),
        };
        let records = raw.iter().map(read_record).collect::<Result<Vec<_>, _>>()?;
        let mut ids = BTreeSet::new();
        if records
            .iter()
            .any(|record| !ids.insert(record.source_record_id.as_str()))
        {
            return Err(SourceContractError::DuplicateIdentity);
        }
        Ok(Self {
            instance_id: text(get(&fields, "instance_id"), 200)?,
            page_kind: kind,
            fetched_at: timestamp(get(&fields, "fetched_at"))?,
            next_cursor,
            records,
            retry_after: optional(get(&fields, "retry_after"))
                .map(|value| timestamp(Some(value)))
                .transpose()?,
        })
    }

    /// Check the source capabilities, half-open query window and cursor chain before publication.
    pub fn check_request(
        &self,
        request: &SourceRequest<'_>,
    ) -> Result<Option<String>, SourceContractError> {
        let declared = request.capabilities;
        if request.instance_id != self.instance_id {
            return Err(SourceContractError::Instance);
        }
        if !declared.observations
            || declared.max_page_size == 0
            || declared.max_page_size > MAX_RECORDS
            || self.records.len() > declared.max_page_size.min(100)
        {
            return Err(SourceContractError::Capacity);
        }
        if request.start >= request.end {
            return Err(SourceContractError::Window);
        }
        if self.page_kind == SourcePageKind::CompleteIntervalSnapshot && request.cursor.is_some() {
            return Err(SourceContractError::Cursor);
        }
        let mut next_digest = None;
        if let Some(next) = self.next_cursor.as_ref() {
            let digest = sha256_hex(next.as_bytes());
            let current_digest = request.cursor.map(|value| sha256_hex(value.as_bytes()));
            let seen = current_digest.as_ref().map(|_| request.previous_cursors);
            let added = usize::from(
                current_digest
                    .as_ref()
                    .is_some_and(|hash| seen.is_none_or(|known| !known.contains(hash))),
            );
            if !declared.cursor
                || current_digest.as_ref() == Some(&digest)
                || seen.is_some_and(|known| known.contains(&digest))
                || seen.map_or(0, BTreeSet::len).saturating_add(added) >= MAX_CURSOR_HISTORY
            {
                return Err(SourceContractError::Cursor);
            }
            next_digest = Some(digest);
        }
        for record in &self.records {
            if record.operation == SourceOperation::Delete && !declared.deletions {
                return Err(SourceContractError::Deletion);
            }
            if declared.time_semantics == SourceTimeSemantics::Interval
                && record.effective_end.is_none()
            {
                return Err(SourceContractError::Interval);
            }
            if !record_overlaps_window(record, declared.time_semantics, request.start, request.end)?
            {
                return Err(SourceContractError::Window);
            }
        }
        Ok(next_digest)
    }
}

pub fn record_overlaps_window(
    record: &SourceRecord,
    semantics: SourceTimeSemantics,
    start: StoredInstant,
    end: StoredInstant,
) -> Result<bool, SourceContractError> {
    let effective = record.effective_at.instant();
    match semantics {
        SourceTimeSemantics::Instant => Ok(start <= effective && effective < end),
        SourceTimeSemantics::Interval => Ok(record
            .effective_end
            .is_some_and(|until| effective < end && until.instant() > start)),
        SourceTimeSemantics::CalendarDay => {
            let source_zone = jiff::tz::db()
                .get(&record.source_timezone)
                .map_err(|_| SourceContractError::Timezone)?;
            let instant = jiff::Timestamp::from_microsecond(effective.unix_microseconds())
                .map_err(|_| SourceContractError::Timestamp)?;
            let day = instant.to_zoned_datetime(source_zone.clone()).date();
            let tomorrow = day.tomorrow().map_err(|_| SourceContractError::Timestamp)?;
            let day_start = source_zone
                .to_ambiguous_timestamp(day.at(0, 0, 0, 0))
                .compatible()
                .map_err(|_| SourceContractError::Timestamp)?;
            let day_end = source_zone
                .to_ambiguous_timestamp(tomorrow.at(0, 0, 0, 0))
                .compatible()
                .map_err(|_| SourceContractError::Timestamp)?;
            Ok(day_start.as_microsecond() < end.unix_microseconds()
                && day_end.as_microsecond() > start.unix_microseconds())
        }
    }
}

type Fields<'a, 's> = BTreeMap<&'a str, &'a JsonValue<'s>>;

fn object<'a, 's>(value: &'a JsonValue<'s>) -> Result<Fields<'a, 's>, SourceContractError> {
    let JsonValue::Object(values) = value else {
        return Err(SourceContractError::Shape);
    };
    Ok(values
        .iter()
        .map(|(name, value)| (name.as_ref(), value))
        .collect())
}

fn check_keys(fields: &Fields<'_, '_>, allowed: &[&str]) -> Result<(), SourceContractError> {
    if fields.keys().any(|name| !allowed.contains(name)) {
        return Err(SourceContractError::Shape);
    }
    Ok(())
}

fn get<'a, 's>(fields: &Fields<'a, 's>, name: &str) -> Option<&'a JsonValue<'s>> {
    fields.get(name).copied()
}

fn string<'a>(value: &'a JsonValue<'_>) -> Option<&'a str> {
    match value {
        JsonValue::Str(value) => Some(value),
        _ => None,
    }
}

fn text(value: Option<&JsonValue<'_>>, cap: usize) -> Result<String, SourceContractError> {
    value
        .and_then(string)
        .filter(|value| (1..=cap).contains(&value.chars().count()))
        .map(str::to_owned)
        .ok_or(SourceContractError::Shape)
}

fn optional<'a, 's>(value: Option<&'a JsonValue<'s>>) -> Option<&'a JsonValue<'s>> {
    value.filter(|value| !matches!(value, JsonValue::Null))
}

fn timestamp(value: Option<&JsonValue<'_>>) -> Result<SourceTimestamp, SourceContractError> {
    let value = match value {
        Some(JsonValue::Int(value)) => json!(value),
        Some(JsonValue::Float(value)) if value.is_finite() => json!(value),
        Some(JsonValue::Str(value)) => Value::String(value.to_string()),
        _ => return Err(SourceContractError::Timestamp),
    };
    let original = parse_aware_datetime(&value).ok_or(SourceContractError::Timestamp)?;
    let instant = as_stored_instant(original).ok_or(SourceContractError::Timestamp)?;
    Ok(SourceTimestamp { original, instant })
}

fn read_record(raw: &JsonValue<'_>) -> Result<SourceRecord, SourceContractError> {
    let fields = object(raw)?;
    check_keys(
        &fields,
        &[
            "source_record_id",
            "operation",
            "observed_at",
            "effective_at",
            "effective_end",
            "source_timezone",
            "source_reference",
            "payload",
        ],
    )?;
    let operation = match get(&fields, "operation") {
        None => SourceOperation::Upsert,
        Some(value) if string(value) == Some("upsert") => SourceOperation::Upsert,
        Some(value) if string(value) == Some("delete") => SourceOperation::Delete,
        _ => return Err(SourceContractError::Shape),
    };
    let effective_at = timestamp(get(&fields, "effective_at"))?;
    let effective_end = optional(get(&fields, "effective_end"))
        .map(|value| timestamp(Some(value)))
        .transpose()?;
    if effective_end.is_some_and(|end| end.instant() <= effective_at.instant()) {
        return Err(SourceContractError::Interval);
    }
    let source_timezone = text(get(&fields, "source_timezone"), 100)?;
    if !known_timezone(&source_timezone) {
        return Err(SourceContractError::Timezone);
    }
    let mut payload = b"{}".to_vec();
    if let Some(raw) = get(&fields, "payload") {
        let JsonValue::Object(values) = raw else {
            return Err(SourceContractError::Payload);
        };
        if operation == SourceOperation::Delete && !values.is_empty() {
            return Err(SourceContractError::Deletion);
        }
        validate_payload(raw, 0)?;
        let mut spaced = Vec::new();
        write_value(&mut spaced, raw, Utf8Layout::Archive)
            .map_err(|_| SourceContractError::Payload)?;
        if spaced.len() > MAX_PAYLOAD_BYTES {
            return Err(SourceContractError::Payload);
        }
        payload.clear();
        write_value(&mut payload, raw, Utf8Layout::Compact)
            .map_err(|_| SourceContractError::Payload)?;
    }
    Ok(SourceRecord {
        source_record_id: text(get(&fields, "source_record_id"), 200)?,
        operation,
        observed_at: timestamp(get(&fields, "observed_at"))?,
        effective_at,
        effective_end,
        source_timezone,
        source_reference: text(get(&fields, "source_reference"), 500)?,
        payload,
    })
}

fn validate_payload(value: &JsonValue<'_>, depth: u8) -> Result<(), SourceContractError> {
    if depth > MAX_DEPTH {
        return Err(SourceContractError::Payload);
    }
    match value {
        JsonValue::Object(_) => {
            let fields = object(value)?;
            if fields.len() > MAX_COLLECTION_ITEMS {
                return Err(SourceContractError::Payload);
            }
            for field in fields.into_values() {
                validate_payload(field, depth + 1)?;
            }
        }
        JsonValue::Array(items) => {
            if items.len() > MAX_COLLECTION_ITEMS {
                return Err(SourceContractError::Payload);
            }
            for item in items.iter() {
                validate_payload(item, depth + 1)?;
            }
        }
        JsonValue::Float(value) if !value.is_finite() => return Err(SourceContractError::Payload),
        _ => (),
    }
    Ok(())
}
