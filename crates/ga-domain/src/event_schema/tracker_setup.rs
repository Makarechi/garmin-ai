use std::{collections::BTreeSet, error::Error, fmt};

use num_bigint::BigInt;
use serde_json::{Map, Number, Value, json};

use crate::{
    event_input::known_timezone,
    event_payload::{legacy_boolean, numeric_text, python_whitespace},
    language_tags::normalize_language_tag,
    legacy_json::{Utf8Layout, encode_ascii},
    metric_contract::metric_unit_dimension,
    sha256_hex,
};

use super::{
    CustomDefinition, CustomDefinitionError,
    custom::{ExactNumber, field_name},
};

#[derive(Debug)]
pub enum TrackerSetupError {
    Invalid,
    Definition(CustomDefinitionError),
}

impl fmt::Display for TrackerSetupError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid => f.write_str("Invalid tracker draft"),
            Self::Definition(error) => error.fmt(f),
        }
    }
}
impl Error for TrackerSetupError {}

pub struct TrackerSetupDraft {
    document: Value,
    definition: CustomDefinition,
    hash: String,
}

pub struct TrackerSettingsUpdate {
    pub revision: Number,
    pub shortcut: Option<String>,
    pub reminder_enabled: bool,
    pub reminder_time: Option<String>,
    pub reminder_timezone: String,
}

impl TrackerSettingsUpdate {
    pub fn from_json(value: &Value) -> Result<Self, TrackerSetupError> {
        use TrackerSetupError::Invalid;
        let raw = fields(
            value,
            &[
                "revision",
                "shortcut",
                "reminder_enabled",
                "reminder_time",
                "reminder_timezone",
            ],
        )?;
        let revision = raw
            .get("revision")
            .and_then(Value::as_number)
            .ok_or(Invalid)?;
        let integer = revision.to_string();
        if !integer.bytes().all(|byte| byte.is_ascii_digit())
            || integer.parse::<BigInt>().map_err(|_| Invalid)? < BigInt::from(1)
        {
            return Err(Invalid);
        }
        let shortcut = optional_name(raw.get("shortcut"), 0, 64)?;
        let reminder_time = optional_name(raw.get("reminder_time"), 5, 5)?;
        let reminder_enabled =
            legacy_boolean(raw.get("reminder_enabled").ok_or(Invalid)?).ok_or(Invalid)?;
        let reminder_timezone = text_or(raw.get("reminder_timezone"), "UTC")?;
        if !known_timezone(reminder_timezone)
            || reminder_time.is_some_and(|value| !clock(value))
            || reminder_enabled && reminder_time.is_none()
        {
            return Err(Invalid);
        }
        Ok(Self {
            revision: revision.clone(),
            shortcut: shortcut.map(str::to_owned),
            reminder_enabled,
            reminder_time: reminder_time.map(str::to_owned),
            reminder_timezone: reminder_timezone.to_owned(),
        })
    }
}

impl TrackerSetupDraft {
    pub fn from_json(value: &Value) -> Result<Self, TrackerSetupError> {
        use TrackerSetupError::Invalid;
        let raw = fields(
            value,
            &[
                "key",
                "name",
                "locale",
                "topology",
                "derived_duration",
                "fields",
                "shortcut",
                "reminder_enabled",
                "reminder_time",
                "reminder_timezone",
                "privacy",
            ],
        )?;
        let key = raw
            .get("key")
            .and_then(Value::as_str)
            .filter(|key| field_name(key))
            .ok_or(Invalid)?;
        let name = name(raw.get("name"), 1, 120)?;
        let locale = normalize_language_tag(text_or(raw.get("locale"), "en")?).ok_or(Invalid)?;
        let topology = text_or(raw.get("topology"), "point")?;
        if !matches!(
            topology,
            "point" | "open_interval" | "bounded_interval" | "flexible"
        ) {
            return Err(Invalid);
        }
        let privacy = text_or(raw.get("privacy"), "private")?;
        let derived_duration = flag(raw.get("derived_duration"), false)?;
        let shortcut = optional_name(raw.get("shortcut"), 0, 64)?;
        let reminder_enabled = flag(raw.get("reminder_enabled"), false)?;
        let reminder_time = optional_name(raw.get("reminder_time"), 5, 5)?;
        let reminder_timezone = text_or(raw.get("reminder_timezone"), "UTC")?;
        if !matches!(privacy, "private" | "sensitive")
            || derived_duration && topology == "point"
            || !known_timezone(reminder_timezone)
            || reminder_time.is_some_and(|value| !clock(value))
            || reminder_enabled && reminder_time.is_none()
        {
            return Err(Invalid);
        }
        let field_list = raw
            .get("fields")
            .and_then(Value::as_array)
            .filter(|fields| (1..=32).contains(&fields.len()))
            .ok_or(Invalid)?;
        let mut required = Vec::new();
        let mut properties = Map::new();
        let mut metadata = Map::new();
        let mut normalized = Vec::with_capacity(field_list.len());
        let key = format!("user.{key}");
        for value in field_list {
            let field = Field::from_json(value)?;
            if properties.contains_key(field.key)
                || derived_duration && field.key == "elapsed_minutes"
                || topology != "bounded_interval"
                    && field.metric_semantics == Some("interval_total")
            {
                return Err(Invalid);
            }
            if field.required {
                required.push(field.key);
            }
            properties.insert(field.key.to_owned(), field.schema());
            metadata.insert(field.key.to_owned(), field.metadata(&key, &locale));
            normalized.push(field.document());
        }
        let document = json!({"key":raw["key"],"name":name,"locale":locale,"topology":topology,
            "derived_duration":derived_duration,"fields":normalized,"shortcut":shortcut,"reminder_enabled":reminder_enabled,
            "reminder_time":reminder_time,"reminder_timezone":reminder_timezone,"privacy":privacy});
        let definition = CustomDefinition::from_json(&json!({
            "key":key,"labels":{locale:name},"schema":{"type":"object","properties":properties,
                "required":required,"additionalProperties":false},"fields":metadata,"topology":topology,
            "derived_duration":derived_duration,"privacy":privacy,
        })).map_err(TrackerSetupError::Definition)?;
        let bytes = serde_json::to_vec(&document).map_err(|_| Invalid)?;
        let ascii = encode_ascii(&bytes, Utf8Layout::Compact, bytes.len()).map_err(|_| Invalid)?;
        let hash = sha256_hex(ascii.as_bytes());
        Ok(Self {
            document,
            definition,
            hash,
        })
    }

    pub fn document(&self) -> &Value {
        &self.document
    }

    pub fn definition(&self) -> &CustomDefinition {
        &self.definition
    }

    pub fn hash(&self) -> &str {
        &self.hash
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Kind {
    Text,
    Number,
    Integer,
    Boolean,
    Choice,
    Scale,
}

impl Kind {
    fn read(value: &Value) -> Option<Self> {
        Some(match value.as_str()? {
            "text" => Self::Text,
            "number" => Self::Number,
            "integer" => Self::Integer,
            "boolean" => Self::Boolean,
            "choice" => Self::Choice,
            "scale" => Self::Scale,
            _ => return None,
        })
    }
    fn label(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Number => "number",
            Self::Integer => "integer",
            Self::Boolean => "boolean",
            Self::Choice => "choice",
            Self::Scale => "scale",
        }
    }
}

struct Field<'a> {
    key: &'a str,
    label: &'a str,
    kind: Kind,
    metric_semantics: Option<&'a str>,
    required: bool,
    unit: Option<&'a str>,
    minimum: Value,
    maximum: Value,
    options: &'a [Value],
    max_length: u32,
}

impl<'a> Field<'a> {
    fn from_json(value: &'a Value) -> Result<Self, TrackerSetupError> {
        use TrackerSetupError::Invalid;
        let raw = fields(
            value,
            &[
                "key",
                "label",
                "kind",
                "metric_semantics",
                "required",
                "unit",
                "minimum",
                "maximum",
                "options",
                "max_length",
            ],
        )?;
        let key = raw
            .get("key")
            .and_then(Value::as_str)
            .filter(|key| field_name(key))
            .ok_or(Invalid)?;
        let label = name(raw.get("label"), 1, 120)?;
        let kind = Kind::read(raw.get("kind").ok_or(Invalid)?).ok_or(Invalid)?;
        let metric_semantics = optional_name(raw.get("metric_semantics"), 1, 32)?;
        let unit = optional_name(raw.get("unit"), 1, 32)?;
        if unit.is_some_and(|unit| {
            !unit.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'%' | b'.' | b'/' | b'-')
            })
        }) {
            return Err(Invalid);
        }
        let options = raw
            .get("options")
            .map_or(Some(&[][..]), |value| value.as_array().map(Vec::as_slice))
            .ok_or(Invalid)?;
        let field = Self {
            key,
            label,
            kind,
            metric_semantics,
            unit,
            required: flag(raw.get("required"), true)?,
            minimum: optional_number(raw.get("minimum"))?,
            maximum: optional_number(raw.get("maximum"))?,
            options,
            max_length: raw
                .get("max_length")
                .map_or(Some(500), |value| {
                    let value = number(value)?;
                    let bound = value.as_f64()?;
                    (bound.fract() == 0.0 && (1.0..=16_000.0).contains(&bound))
                        .then_some(bound as u32)
                })
                .ok_or(Invalid)?,
        };
        field.validate()?;
        Ok(field)
    }

    fn validate(&self) -> Result<(), TrackerSetupError> {
        use TrackerSetupError::Invalid;
        if matches!(self.kind, Kind::Number | Kind::Integer | Kind::Scale) {
            let min = ExactNumber::read(&self.minimum).ok_or(Invalid)?;
            let max = ExactNumber::read(&self.maximum).ok_or(Invalid)?;
            let lo = self
                .minimum
                .as_f64()
                .filter(|value| value.is_finite())
                .ok_or(Invalid)?;
            let hi = self
                .maximum
                .as_f64()
                .filter(|value| value.is_finite())
                .ok_or(Invalid)?;
            if min > max
                || (self.kind != Kind::Number
                    && (min.floor() != min.ceil()
                        || max.floor() != max.ceil()
                        || min.floor() < BigInt::from(-(1i64 << 53))
                        || max.ceil() > BigInt::from(1i64 << 53)
                        || self.kind == Kind::Scale && hi - lo > 20.0))
            {
                return Err(Invalid);
            }
        } else if !self.minimum.is_null() || !self.maximum.is_null() {
            return Err(Invalid);
        }
        if self.kind == Kind::Choice {
            let options = self
                .options
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .filter(|text| {
                            !text.trim_matches(python_whitespace).is_empty()
                                && text.chars().count() <= 120
                        })
                        .ok_or(Invalid)
                })
                .collect::<Result<BTreeSet<_>, _>>()?;
            if !(1..=50).contains(&options.len()) || options.len() != self.options.len() {
                return Err(Invalid);
            }
        } else if !self.options.is_empty() {
            return Err(Invalid);
        }
        if self.kind == Kind::Number && self.unit.is_none() {
            return Err(Invalid);
        }
        if let Some(unit) = self.unit {
            if matches!(self.kind, Kind::Text | Kind::Boolean | Kind::Choice) {
                return Err(Invalid);
            }
            if matches!(self.kind, Kind::Number | Kind::Integer)
                && (metric_unit_dimension(unit).is_none()
                    || unit.starts_with("score_")
                        && !matches!(unit, "score_1-5" | "score_1-7" | "score_1-10"))
            {
                return Err(Invalid);
            }
        }
        if let Some(semantics) = self.metric_semantics
            && (!matches!(self.kind, Kind::Number | Kind::Integer)
                || !matches!(
                    semantics,
                    "gauge"
                        | "event_total"
                        | "event_count"
                        | "interval_total"
                        | "cumulative_counter"
                )
                || semantics == "event_count"
                    && (self.kind != Kind::Integer
                        || !matches!(self.unit, None | Some("count" | "steps")))
                || semantics != "gauge" && self.minimum.as_f64().is_some_and(|value| value < 0.0))
        {
            return Err(Invalid);
        }
        Ok(())
    }

    fn schema(&self) -> Value {
        match self.kind {
            Kind::Text => {
                json!({"type":"string","minLength":u8::from(self.required),"maxLength":self.max_length})
            }
            Kind::Choice => json!({"type":"string","enum":self.options}),
            Kind::Boolean => json!({"type":"boolean"}),
            kind => {
                json!({"type":if kind==Kind::Number {"number"}else{"integer"},"minimum":self.minimum,"maximum":self.maximum})
            }
        }
    }

    fn metadata(&self, key: &str, locale: &str) -> Value {
        let semantic = match self.kind {
            Kind::Text => "text",
            Kind::Choice => "nominal",
            Kind::Boolean => "boolean",
            Kind::Number => "quantity",
            Kind::Integer => "count",
            Kind::Scale => "ordinal",
        };
        let unit = match self.kind {
            Kind::Scale => self
                .minimum
                .as_f64()
                .zip(self.maximum.as_f64())
                .map(|(lo, hi)| format!("score_{}-{}", lo as i64, hi as i64)),
            Kind::Integer => Some(self.unit.unwrap_or("count").to_owned()),
            _ => self.unit.map(str::to_owned),
        };
        json!({"id":format!("{key}.{}",self.key),"labels":{locale:self.label},"semantic":semantic,"unit":unit,"metric_semantics":self.metric_semantics})
    }

    fn document(&self) -> Value {
        json!({"key":self.key,"label":self.label,"kind":self.kind.label(),"metric_semantics":self.metric_semantics,
            "required":self.required,"unit":self.unit,"minimum":self.minimum,"maximum":self.maximum,
            "options":self.options,"max_length":self.max_length})
    }
}

fn fields<'a>(
    value: &'a Value,
    allowed: &[&str],
) -> Result<&'a Map<String, Value>, TrackerSetupError> {
    value
        .as_object()
        .filter(|fields| fields.keys().all(|key| allowed.contains(&key.as_str())))
        .ok_or(TrackerSetupError::Invalid)
}

fn name(value: Option<&Value>, min: usize, max: usize) -> Result<&str, TrackerSetupError> {
    value
        .and_then(Value::as_str)
        .filter(|text| (min..=max).contains(&text.chars().count()))
        .ok_or(TrackerSetupError::Invalid)
}

fn optional_name(
    value: Option<&Value>,
    min: usize,
    max: usize,
) -> Result<Option<&str>, TrackerSetupError> {
    value
        .filter(|value| !value.is_null())
        .map(|value| name(Some(value), min, max))
        .transpose()
}

fn text_or<'a>(value: Option<&'a Value>, default: &'a str) -> Result<&'a str, TrackerSetupError> {
    value
        .map_or(Some(default), Value::as_str)
        .ok_or(TrackerSetupError::Invalid)
}

fn flag(value: Option<&Value>, default: bool) -> Result<bool, TrackerSetupError> {
    value
        .map_or(Some(default), legacy_boolean)
        .ok_or(TrackerSetupError::Invalid)
}

fn clock(value: &str) -> bool {
    let Some((hour, minute)) = value.split_once(':') else {
        return false;
    };
    hour.len() == 2
        && minute.len() == 2
        && value.bytes().all(|b| b == b':' || b.is_ascii_digit())
        && hour.parse::<u8>().is_ok_and(|n| n < 24)
        && minute.parse::<u8>().is_ok_and(|n| n < 60)
}

fn optional_number(value: Option<&Value>) -> Result<Value, TrackerSetupError> {
    value
        .filter(|value| !value.is_null())
        .map(|value| {
            number(value)
                .map(Value::Number)
                .ok_or(TrackerSetupError::Invalid)
        })
        .transpose()
        .map(Option::unwrap_or_default)
}

fn number(value: &Value) -> Option<Number> {
    match value {
        Value::Number(number) => number
            .as_f64()
            .filter(|number| number.is_finite())
            .map(|_| number.clone()),
        Value::Bool(flag) => Some(u8::from(*flag).into()),
        Value::String(text) => {
            let text = numeric_text(text)?;
            let integer = text
                .split_once('.')
                .filter(|(_, fraction)| {
                    !fraction.is_empty() && fraction.bytes().all(|digit| digit == b'0')
                })
                .map_or(text.as_ref(), |(integer, _)| integer);
            if let Ok(integer) = integer.parse::<BigInt>() {
                integer.to_string().parse().ok()
            } else {
                Number::from_f64(text.parse().ok()?)
            }
        }
        _ => None,
    }
}
