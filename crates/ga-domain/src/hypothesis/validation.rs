use serde_json::{Map, Value, json};
use speedate::{DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig};
use strum::EnumMessage;
use time::{Date, Month};
use uuid::Uuid;

use super::{InvalidHypothesisSpec, float_argument};

const FIELDS: &[&str] = &[
    "id",
    "question",
    "discovery_start",
    "discovery_end",
    "validation_start",
    "validation_end",
    "expires",
    "timezone",
    "outcome",
    "late_hours",
    "direction",
];

pub(super) struct FieldError {
    kind: &'static str,
    message: String,
    context: Option<Value>,
}

impl FieldError {
    fn new(kind: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            context: None,
        }
    }

    fn context(mut self, context: Value) -> Self {
        self.context = Some(context);
        self
    }

    fn document(self, location: &[&str], input: &Value) -> Value {
        let mut document = json!({
            "type":self.kind,"loc":location,"msg":self.message,"input":input,
        });
        if let Some(context) = self.context {
            document["ctx"] = context;
        }
        document
    }
}

pub(super) struct Body<'a> {
    input: &'a Value,
    fields: &'a Map<String, Value>,
    errors: Vec<Value>,
}

impl<'a> Body<'a> {
    pub(super) fn new(input: &'a Value) -> Result<Self, InvalidHypothesisSpec> {
        let fields = input.as_object().ok_or_else(|| {
            let error = if input.is_null() {
                missing()
            } else {
                FieldError::new(
                    "model_attributes_type",
                    "Input should be a valid dictionary or object to extract fields from",
                )
            };
            InvalidHypothesisSpec(vec![error.document(&["body"], input)])
        })?;
        Ok(Self {
            input,
            fields,
            errors: Vec::new(),
        })
    }

    pub(super) fn required<T>(
        &mut self,
        key: &str,
        parse: impl FnOnce(&'a Value) -> Result<T, FieldError>,
    ) -> Option<T> {
        match self.fields.get(key) {
            Some(input) => self.parse(key, input, parse),
            None => {
                self.errors
                    .push(missing().document(&["body", key], self.input));
                None
            }
        }
    }

    pub(super) fn optional<T>(
        &mut self,
        key: &str,
        default: T,
        parse: impl FnOnce(&'a Value) -> Result<T, FieldError>,
    ) -> Option<T> {
        self.fields
            .get(key)
            .map_or(Some(default), |input| self.parse(key, input, parse))
    }

    fn parse<T>(
        &mut self,
        key: &str,
        input: &'a Value,
        parse: impl FnOnce(&'a Value) -> Result<T, FieldError>,
    ) -> Option<T> {
        match parse(input) {
            Ok(value) => Some(value),
            Err(error) => {
                self.errors.push(error.document(&["body", key], input));
                None
            }
        }
    }

    pub(super) fn check_fields(mut self) -> Result<(), InvalidHypothesisSpec> {
        for (key, input) in self
            .fields
            .iter()
            .filter(|(key, _)| !FIELDS.contains(&key.as_str()))
        {
            self.errors.push(
                FieldError::new("extra_forbidden", "Extra inputs are not permitted")
                    .document(&["body", key], input),
            );
        }
        if self.errors.is_empty() {
            return Ok(());
        }
        Err(InvalidHypothesisSpec(self.errors))
    }
}

fn missing() -> FieldError {
    FieldError::new("missing", "Field required")
}

pub(super) fn invalid_protocol(reason: &str, input: &Value) -> InvalidHypothesisSpec {
    InvalidHypothesisSpec(vec![
        FieldError::new("value_error", format!("Value error, {reason}"))
            .context(json!({"error":{}}))
            .document(&["body"], input),
    ])
}

pub(super) fn id(input: &Value) -> Result<Uuid, FieldError> {
    let text = input.as_str().ok_or_else(|| {
        FieldError::new(
            "uuid_type",
            "UUID input should be a string, bytes or UUID object",
        )
    })?;
    Uuid::parse_str(text).map_err(|_| uuid_error(text))
}

pub(super) fn path_id(value: &str) -> Result<Uuid, InvalidHypothesisSpec> {
    Uuid::parse_str(value).map_err(|_| {
        let input = Value::String(value.to_owned());
        InvalidHypothesisSpec(vec![
            uuid_error(value).document(&["path", "identity"], &input),
        ])
    })
}

fn uuid_error(value: &str) -> FieldError {
    let bare = value
        .strip_prefix("urn:uuid:")
        .or_else(|| {
            value
                .strip_prefix('{')
                .and_then(|value| value.strip_suffix('}'))
        })
        .unwrap_or(value);
    let reason = if let Some((index, invalid)) = bare
        .char_indices()
        .find(|&(_, c)| c != '-' && !c.is_ascii_hexdigit())
    {
        format!("invalid character: found `{invalid}` at {index}")
    } else if bare.len() != 36 && !bare.contains('-') {
        format!("invalid length: found {}", bare.len())
    } else {
        let groups: Vec<_> = bare.split('-').map(str::len).collect();
        if groups.len() != 5 {
            format!("invalid group count: expected 5, found {}", groups.len())
        } else if let Some((position, (found, expected))) = groups
            .into_iter()
            .zip([8, 4, 4, 4, 12])
            .enumerate()
            .find(|(_, (found, expected))| found != expected)
        {
            format!("invalid group length in group {position}: expected {expected}, found {found}")
        } else {
            format!("invalid length: found {}", value.len())
        }
    };
    FieldError::new(
        "uuid_parsing",
        format!("Input should be a valid UUID, {reason}"),
    )
    .context(json!({"error":reason}))
}

pub(super) fn text(input: &Value) -> Result<&str, FieldError> {
    input
        .as_str()
        .ok_or_else(|| FieldError::new("string_type", "Input should be a valid string"))
}

pub(super) fn question(input: &Value) -> Result<&str, FieldError> {
    let question = text(input)?;
    match question.chars().count() {
        0 => Err(FieldError::new(
            "string_too_short",
            "String should have at least 1 character",
        )
        .context(json!({"min_length":1}))),
        1..=1000 => Ok(question),
        _ => Err(FieldError::new(
            "string_too_long",
            "String should have at most 1000 characters",
        )
        .context(json!({"max_length":1000}))),
    }
}

pub(super) fn literal<'a>(input: &'a Value, choices: [&str; 2]) -> Result<&'a str, FieldError> {
    input
        .as_str()
        .filter(|value| choices.contains(value))
        .ok_or_else(|| {
            let expected = format!("'{}' or '{}'", choices[0], choices[1]);
            FieldError::new("literal_error", format!("Input should be {expected}"))
                .context(json!({"expected":expected}))
        })
}

pub(super) fn hours(input: &Value) -> Result<f64, FieldError> {
    let number = float_argument(input).ok_or_else(|| {
        if input.is_string() {
            FieldError::new(
                "float_parsing",
                "Input should be a valid number, unable to parse string as a number",
            )
        } else {
            FieldError::new("float_type", "Input should be a valid number")
        }
    })?;
    if number.is_nan() || number <= 0.0 {
        return Err(
            FieldError::new("greater_than", "Input should be greater than 0")
                .context(json!({"gt":0.0})),
        );
    }
    if number > 24.0 {
        return Err(FieldError::new(
            "less_than_equal",
            "Input should be less than or equal to 24",
        )
        .context(json!({"le":24.0})));
    }
    Ok(number)
}

pub(super) fn date(input: &Value) -> Result<Date, FieldError> {
    let bytes = match input {
        Value::String(value) => value.as_bytes(),
        Value::Number(value) => value.as_str().as_bytes(),
        _ => return Err(FieldError::new("date_type", "Input should be a valid date")),
    };
    let parsed = speedate::Date::parse_bytes(bytes).or_else(|_| {
        let config = DateTimeConfig {
            time_config: TimeConfig {
                microseconds_precision_overflow_behavior:
                    MicrosecondsPrecisionOverflowBehavior::Truncate,
                ..TimeConfig::default()
            },
            ..DateTimeConfig::default()
        };
        let stamp =
            DateTime::parse_bytes_with_config(bytes, &config).or_else(|error| {
                match input.as_f64() {
                    Some(number) => DateTime::from_float_with_config(number, &config),
                    None => Err(error),
                }
            })?;
        if stamp.time.hour != 0
            || stamp.time.minute != 0
            || stamp.time.second != 0
            || stamp.time.microsecond != 0
        {
            return Err(speedate::ParseError::DateNotExact);
        }
        Ok(stamp.date)
    });
    let parsed = parsed.map_err(|error| {
        if error == speedate::ParseError::DateNotExact {
            return FieldError::new(
                "date_from_datetime_inexact",
                "Datetimes provided to dates should have zero time - e.g. be exact dates",
            );
        }
        let description = error.get_documentation().unwrap_or("invalid date");
        FieldError::new(
            "date_from_datetime_parsing",
            format!("Input should be a valid date or datetime, {description}"),
        )
        .context(json!({"error":description}))
    })?;
    let out_of_range = || {
        FieldError::new(
            "date_parsing",
            "Input should be a valid date in the format YYYY-MM-DD, year 0 is out of range",
        )
        .context(json!({"error":"year 0 is out of range"}))
    };
    if parsed.year == 0 {
        return Err(out_of_range());
    }
    let month = Month::try_from(parsed.month).map_err(|_| out_of_range())?;
    Date::from_calendar_date(i32::from(parsed.year), month, parsed.day).map_err(|_| out_of_range())
}
