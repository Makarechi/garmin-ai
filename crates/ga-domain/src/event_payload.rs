use std::{borrow::Cow, error::Error, fmt};

use jiter::JsonValue;
use serde_json::{Map, Number, Value};
use uuid::Uuid;

pub struct SystemPayload {
    kind: String,
    json: Value,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvalidPayload {
    InputTooLarge,
    InvalidObject,
    UnknownType,
    UnknownField,
    InvalidField(&'static str),
    EmptyObservation,
    DoseRange,
}

impl fmt::Display for InvalidPayload {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InputTooLarge => "Diary payload exceeds the byte limit",
            Self::InvalidObject => "Diary payload must be a JSON object",
            Self::UnknownType => "Diary payload type is unsupported",
            Self::UnknownField => "Diary payload contains an unknown field",
            Self::InvalidField(field) => return write!(formatter, "Invalid diary field {field}"),
            Self::EmptyObservation => "An explicit diary observation is required",
            Self::DoseRange => "Invalid caffeine range or estimate",
        })
    }
}

impl Error for InvalidPayload {}

impl SystemPayload {
    pub fn from_json(bytes: &[u8], max_bytes: usize) -> Result<Self, InvalidPayload> {
        if bytes.len() > max_bytes {
            return Err(InvalidPayload::InputTooLarge);
        }
        let input = JsonValue::parse(bytes, true).map_err(|_| InvalidPayload::InvalidObject)?;
        let value = json_value(&input).ok_or(InvalidPayload::InvalidObject)?;
        Self::from_value(value)
    }

    pub fn from_value(input: Value) -> Result<Self, InvalidPayload> {
        let Value::Object(mut input) = input else {
            return Err(InvalidPayload::InvalidObject);
        };
        let Some(Value::String(kind)) = input.remove("type") else {
            return Err(InvalidPayload::UnknownType);
        };
        let mut fields = PayloadFields {
            input,
            output: Map::new(),
        };
        match kind.as_str() {
            "caffeine" => caffeine(&mut fields)?,
            "medication" => medication(&mut fields)?,
            "migraine" => {
                symptom_fields(&mut fields)?;
                fields.optional("notes", text(0, 4000))?;
            }
            "symptom_observation" => {
                fields.required("episode_id", identifier)?;
                symptom_fields(&mut fields)?;
                fields.optional("impact", nonblank_text(1000))?;
                let output = &fields.output;
                if output["severity"].is_null()
                    && output["aura"].is_null()
                    && output["symptoms"].as_array().is_none_or(Vec::is_empty)
                    && output["impact"].is_null()
                {
                    return Err(InvalidPayload::EmptyObservation);
                }
            }
            "activity_effort" => {
                fields.required("activity_id", text(1, 100))?;
                fields.required("perceived_exertion", strict_rating)?;
                fields.optional("notes", text(0, 2000))?;
            }
            "headache_observation" => {
                fields.required("headache", choice(&["yes", "no", "unknown"]))?;
                fields.required("migraine", choice(&["yes", "no", "unknown"]))?;
            }
            "wellbeing_observation" => wellbeing(&mut fields)?,
            "alcohol"
            | "meal"
            | "hydration"
            | "illness"
            | "nap"
            | "stressor"
            | "travel"
            | "mood"
            | "note"
            | "context"
            | "caffeine_absence"
            | "caffeine_log_complete" => {
                fields.required("description", text(1, 4000))?;
                fields.optional("amount", finite_number(0.0, f64::MAX, true))?;
                fields.optional("unit", text(0, 50))?;
                fields.default("tags", Value::Array(Vec::new()), tags)?;
            }
            _ => return Err(InvalidPayload::UnknownType),
        }
        if !fields.input.is_empty() {
            return Err(InvalidPayload::UnknownField);
        }
        fields
            .output
            .insert("type".to_owned(), Value::String(kind.clone()));
        Ok(Self {
            kind,
            json: Value::Object(fields.output),
        })
    }

    pub fn kind(&self) -> &str {
        &self.kind
    }

    pub fn json(&self) -> &Value {
        &self.json
    }
}

pub(crate) fn json_value(value: &JsonValue<'_>) -> Option<Value> {
    match value {
        JsonValue::Null => Some(Value::Null),
        JsonValue::Bool(value) => Some(Value::Bool(*value)),
        JsonValue::Int(value) => Some(Value::from(*value)),
        JsonValue::BigInt(value) => {
            Number::from_f64(value.to_string().parse().ok()?).map(Value::Number)
        }
        JsonValue::Float(value) => Number::from_f64(*value).map(Value::Number),
        JsonValue::Str(value) => Some(Value::String(value.to_string())),
        JsonValue::Array(values) => values
            .iter()
            .map(json_value)
            .collect::<Option<_>>()
            .map(Value::Array),
        JsonValue::Object(fields) => fields
            .iter()
            .map(|(key, value)| Some((key.to_string(), json_value(value)?)))
            .collect::<Option<_>>()
            .map(Value::Object),
    }
}

struct PayloadFields {
    input: Map<String, Value>,
    output: Map<String, Value>,
}

impl PayloadFields {
    fn required(
        &mut self,
        key: &'static str,
        parse: impl FnOnce(Value) -> Option<Value>,
    ) -> Result<(), InvalidPayload> {
        self.apply(key, None, parse)
    }

    fn optional(
        &mut self,
        key: &'static str,
        parse: impl FnOnce(Value) -> Option<Value>,
    ) -> Result<(), InvalidPayload> {
        self.default(key, Value::Null, parse)
    }

    fn default(
        &mut self,
        key: &'static str,
        value: Value,
        parse: impl FnOnce(Value) -> Option<Value>,
    ) -> Result<(), InvalidPayload> {
        self.apply(key, Some(value), parse)
    }

    fn apply(
        &mut self,
        key: &'static str,
        default: Option<Value>,
        parse: impl FnOnce(Value) -> Option<Value>,
    ) -> Result<(), InvalidPayload> {
        let value = match self.input.remove(key) {
            None => default,
            Some(Value::Null) if default == Some(Value::Null) => default,
            Some(value) => parse(value),
        }
        .ok_or(InvalidPayload::InvalidField(key))?;
        self.output.insert(key.to_owned(), value);
        Ok(())
    }
}

fn caffeine(fields: &mut PayloadFields) -> Result<(), InvalidPayload> {
    fields.required("beverage", text(1, 200))?;
    fields.default(
        "servings",
        Value::from(1.0),
        finite_number(0.0, 30.0, false),
    )?;
    fields.default(
        "dose_basis",
        Value::from("unknown"),
        choice(&["total", "per_serving", "unknown"]),
    )?;
    fields.default(
        "dose_provenance",
        Value::from("unknown"),
        choice(&["estimated", "reported_label", "unknown"]),
    )?;
    fields.optional("dose_notes", text(0, 500))?;
    for key in ["caffeine_mg_min", "caffeine_mg_estimate", "caffeine_mg_max"] {
        fields.optional(key, finite_number(0.0, 5000.0, true))?;
    }
    let values = &fields.output;
    let min = values["caffeine_mg_min"].as_f64();
    let estimate = values["caffeine_mg_estimate"].as_f64();
    let max = values["caffeine_mg_max"].as_f64();
    if min.zip(max).is_some_and(|(min, max)| min > max)
        || min
            .zip(estimate)
            .is_some_and(|(min, estimate)| min > estimate)
        || estimate
            .zip(max)
            .is_some_and(|(estimate, max)| estimate > max)
    {
        return Err(InvalidPayload::DoseRange);
    }
    Ok(())
}

fn medication(fields: &mut PayloadFields) -> Result<(), InvalidPayload> {
    fields.optional("name", text(1, 200))?;
    fields.optional("dose", finite_number(0.0, 100_000.0, false))?;
    fields.optional(
        "unit",
        choice(&["mg", "mcg", "g", "ml", "tablet", "drop", "IU"]),
    )?;
    fields.optional("reason_event_id", identifier)
}

fn symptom_fields(fields: &mut PayloadFields) -> Result<(), InvalidPayload> {
    fields.optional("severity", rating)?;
    fields.optional("aura", boolean)?;
    fields.default("symptoms", Value::Array(Vec::new()), tags)
}

fn wellbeing(fields: &mut PayloadFields) -> Result<(), InvalidPayload> {
    for key in ["energy", "restedness", "pain", "functional_impact"] {
        fields.optional(key, strict_rating)?;
    }
    fields.optional("notes", nonblank_text(4000))?;
    if fields.output.values().all(Value::is_null) {
        return Err(InvalidPayload::EmptyObservation);
    }
    Ok(())
}

fn text(min: usize, max: usize) -> impl FnOnce(Value) -> Option<Value> {
    move |value| {
        (min..=max)
            .contains(&value.as_str()?.chars().count())
            .then_some(value)
    }
}

fn nonblank_text(max: usize) -> impl FnOnce(Value) -> Option<Value> {
    move |value| {
        if value.as_str()?.trim_matches(python_whitespace).is_empty() {
            return None;
        }
        text(1, max)(value)
    }
}

fn choice<'a>(choices: &'a [&str]) -> impl FnOnce(Value) -> Option<Value> + 'a {
    |value| choices.contains(&value.as_str()?).then_some(value)
}

fn identifier(value: Value) -> Option<Value> {
    let identifier: Uuid = value.as_str()?.parse().ok()?;
    Some(Value::String(identifier.to_string()))
}

fn tags(value: Value) -> Option<Value> {
    let tags = value.as_array()?;
    (tags.len() <= 30 && tags.iter().all(Value::is_string)).then_some(value)
}

fn strict_rating(value: Value) -> Option<Value> {
    let rating = value.as_u64()?;
    (rating <= 10).then_some(value)
}

fn rating(value: Value) -> Option<Value> {
    let rating: i64 = match value {
        Value::String(raw) => {
            let text = raw.trim();
            let integer = match text.split_once('.') {
                Some((whole, decimals))
                    if !decimals.is_empty() && decimals.bytes().all(|byte| byte == b'0') =>
                {
                    whole
                }
                Some(_) => return None,
                None => text,
            };
            let integer = numeric_text(integer)?;
            integer.parse().ok()?
        }
        Value::Bool(boolean) => i64::from(boolean),
        _ => {
            let number = value.as_f64()?;
            if !(0.0..=10.0).contains(&number) || number.fract() != 0.0 {
                return None;
            }
            number as i64
        }
    };
    (0..=10).contains(&rating).then_some(Value::from(rating))
}

pub(crate) fn boolean(value: Value) -> Option<Value> {
    let converted = match value {
        Value::Bool(value) => value,
        Value::String(value) => match value.to_ascii_lowercase().as_str() {
            "1" | "on" | "t" | "true" | "y" | "yes" => true,
            "0" | "off" | "f" | "false" | "n" | "no" => false,
            _ => return None,
        },
        Value::Number(value) => match value.as_f64()? {
            0.0 => false,
            1.0 => true,
            _ => return None,
        },
        _ => return None,
    };
    Some(Value::Bool(converted))
}

pub(crate) fn finite_number(
    min: f64,
    max: f64,
    inclusive: bool,
) -> impl FnOnce(Value) -> Option<Value> {
    move |value| {
        let number = match value {
            Value::Bool(boolean) => f64::from(u8::from(boolean)),
            Value::String(text) => numeric_text(&text)?.parse().ok()?,
            value => value.as_f64()?,
        };
        if !number.is_finite() || number > max || number < min || (!inclusive && number == min) {
            return None;
        }
        Some(Value::Number(Number::from_f64(number)?))
    }
}

pub(crate) fn numeric_text(text: &str) -> Option<Cow<'_, str>> {
    let text = text.trim();
    if !text.contains('_') {
        return Some(Cow::Borrowed(text));
    }
    let bytes = text.as_bytes();
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'_'
            && (index == 0
                || !bytes[index - 1].is_ascii_digit()
                || !bytes.get(index + 1).is_some_and(u8::is_ascii_digit))
        {
            return None;
        }
    }
    Some(Cow::Owned(text.replace('_', "")))
}

pub(crate) fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{001c}'..='\u{001f}')
}
