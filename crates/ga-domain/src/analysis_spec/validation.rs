use serde_json::{Value, json};
use speedate::{Date, DateTime, ParseError};
use strum::EnumMessage;

use super::{AnalysisOperation, valid_key, valid_method};
use crate::{event_time::LEGACY_TIME, legacy_json::integer_argument};

const OPERATIONS: &str = "'query_entries', 'query_observations', 'aggregate_metric', 'compare_periods', 'query_completeness', 'threshold_segments' or 'threshold_by_local_hour'";
const KEY_PATTERN: &str = r"^(?:user|system)\.[a-z][a-z0-9_.-]{0,126}$";
const METHOD_PATTERN: &str = r"^[a-z_]{1,32}$";

struct Field {
    name: &'static str,
    presence: Presence,
    validate: fn(&Value) -> Option<FieldError>,
}

enum Presence {
    Required,
    Defaulted,
    Optional,
}

use Presence::{Defaulted, Optional, Required};

impl Field {
    const fn new(
        name: &'static str,
        presence: Presence,
        validate: fn(&Value) -> Option<FieldError>,
    ) -> Self {
        Self {
            name,
            presence,
            validate,
        }
    }
}

const FIELDS: &[Field] = &[
    Field::new("operation", Required, operation),
    Field::new("metric_key", Optional, key),
    Field::new("definition_key", Optional, key),
    Field::new("start", Required, datetime),
    Field::new("end", Required, datetime),
    Field::new("comparison_start", Optional, datetime),
    Field::new("comparison_end", Optional, datetime),
    Field::new("method", Optional, method),
    Field::new("metric_version", Optional, |value| integer(value, None)),
    Field::new("source", Optional, |value| bounded_text(value, 0, 200)),
    Field::new("limit", Defaulted, |value| integer(value, Some(1000))),
    Field::new("knowledge_cutoff", Required, datetime),
    Field::new("time_relation", Defaulted, relation),
    Field::new("threshold", Optional, threshold),
    Field::new("threshold_operator", Optional, operator),
    Field::new("timezone", Optional, |value| bounded_text(value, 1, 100)),
];

pub(super) fn known_field(name: &str) -> bool {
    FIELDS.iter().any(|field| field.name == name)
}

pub(super) fn field_errors(input: &Value) -> Vec<Value> {
    let Some(fields) = input.as_object() else {
        return vec![
            FieldError::new(
                "model_type",
                "Input should be a valid dictionary or instance of AnalysisSpec",
            )
            .context(json!({"class_name": "AnalysisSpec"}))
            .document(&["spec"], input),
        ];
    };
    let mut errors = Vec::new();
    for field in FIELDS {
        let (error, submitted) = match fields.get(field.name) {
            None if matches!(field.presence, Required) => {
                (Some(FieldError::new("missing", "Field required")), input)
            }
            None | Some(Value::Null) if matches!(field.presence, Optional) => continue,
            None => continue,
            Some(value) => ((field.validate)(value), value),
        };
        if let Some(error) = error {
            errors.push(error.document(&["spec", field.name], submitted));
        }
    }
    errors.extend(
        fields
            .iter()
            .filter(|(key, _)| !known_field(key))
            .map(|(key, value)| {
                FieldError::new("extra_forbidden", "Extra inputs are not permitted")
                    .document(&["spec", key], value)
            }),
    );
    errors
}

struct FieldError {
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
            "type": self.kind, "loc": location, "msg": self.message, "input": input,
        });
        if let Some(context) = self.context {
            document["ctx"] = context;
        }
        document
    }
}

fn operation(value: &Value) -> Option<FieldError> {
    if value.as_str().and_then(AnalysisOperation::parse).is_some() {
        return None;
    }
    Some(literal_error(OPERATIONS))
}

fn relation(value: &Value) -> Option<FieldError> {
    match value.as_str() {
        Some("starts_within" | "overlap") => None,
        _ => Some(literal_error("'starts_within' or 'overlap'")),
    }
}

fn operator(value: &Value) -> Option<FieldError> {
    match value.as_str() {
        Some("gt" | "gte" | "lt" | "lte") => None,
        _ => Some(literal_error("'gt', 'gte', 'lt' or 'lte'")),
    }
}

fn literal_error(expected: &'static str) -> FieldError {
    FieldError::new("literal_error", format!("Input should be {expected}"))
        .context(json!({"expected": expected}))
}

fn key(value: &Value) -> Option<FieldError> {
    pattern(value, KEY_PATTERN, valid_key)
}

fn method(value: &Value) -> Option<FieldError> {
    pattern(value, METHOD_PATTERN, valid_method)
}

fn pattern(value: &Value, pattern: &'static str, matches: fn(&str) -> bool) -> Option<FieldError> {
    match value.as_str() {
        Some(text) if matches(text) => None,
        Some(_) => Some(
            FieldError::new(
                "string_pattern_mismatch",
                format!("String should match pattern '{pattern}'"),
            )
            .context(json!({"pattern": pattern})),
        ),
        None => Some(string_type()),
    }
}

fn bounded_text(value: &Value, minimum: usize, maximum: usize) -> Option<FieldError> {
    let Some(value) = value.as_str() else {
        return Some(string_type());
    };
    let length = value.chars().count();
    if length < minimum {
        Some(
            FieldError::new(
                "string_too_short",
                "String should have at least 1 character",
            )
            .context(json!({"min_length": minimum})),
        )
    } else if length > maximum {
        Some(
            FieldError::new(
                "string_too_long",
                format!("String should have at most {maximum} characters"),
            )
            .context(json!({"max_length": maximum})),
        )
    } else {
        None
    }
}

fn string_type() -> FieldError {
    FieldError::new("string_type", "Input should be a valid string")
}

fn integer(value: &Value, maximum: Option<u16>) -> Option<FieldError> {
    match integer_argument(value) {
        Some(number) if number < 1.into() => Some(
            FieldError::new(
                "greater_than_equal",
                "Input should be greater than or equal to 1",
            )
            .context(json!({"ge": 1})),
        ),
        Some(number) => maximum.filter(|max| number > (*max).into()).map(|max| {
            FieldError::new(
                "less_than_equal",
                format!("Input should be less than or equal to {max}"),
            )
            .context(json!({"le": max}))
        }),
        None => Some(integer_parsing(value)),
    }
}

fn integer_parsing(value: &Value) -> FieldError {
    if value.as_f64().is_some_and(|number| number.fract() != 0.0) {
        return FieldError::new(
            "int_from_float",
            "Input should be a valid integer, got a number with a fractional part",
        );
    }
    if value.is_number() || value.as_str().is_some_and(oversized_integer_prefix) {
        return FieldError::new(
            "int_parsing_size",
            "Unable to parse input string as an integer, exceeded maximum size",
        );
    }
    if value.is_string() {
        return FieldError::new(
            "int_parsing",
            "Input should be a valid integer, unable to parse string as an integer",
        );
    }
    FieldError::new("int_type", "Input should be a valid integer")
}

fn oversized_integer_prefix(input: &str) -> bool {
    let input = input.trim();
    let sign = usize::from(input.starts_with('-'));
    let digits = input.strip_prefix(['+', '-']).unwrap_or(input);
    let mut significant = 0;
    let mut after_digit = false;
    for byte in digits.bytes() {
        match byte {
            b'0'..=b'9' => {
                after_digit = true;
                if significant > 0 || byte != b'0' {
                    significant += 1;
                    if significant + sign > 4300 {
                        return true;
                    }
                }
            }
            b'_' if after_digit => after_digit = false,
            _ => break,
        }
    }
    false
}

fn threshold(value: &Value) -> Option<FieldError> {
    if let Some(number) = value.as_f64()
        && number.is_finite()
    {
        return None;
    }
    Some(FieldError::new(
        "float_type",
        "Input should be a valid number",
    ))
}

fn datetime(value: &Value) -> Option<FieldError> {
    let parsed = match value {
        Value::String(value) => {
            match DateTime::parse_bytes_with_config(value.as_bytes(), &LEGACY_TIME) {
                parsed @ Ok(_) => parsed,
                Err(_) => {
                    return Some(match Date::parse_bytes(value.as_bytes()) {
                        Ok(_) => timezone_aware(),
                        Err(error) => datetime_parsing(
                            "datetime_from_date_parsing",
                            "datetime or date",
                            parse_reason(&error),
                        ),
                    });
                }
            }
        }
        Value::Number(number) => match number.as_i64() {
            Some(integer) => DateTime::from_timestamp_with_config(integer, 0, &LEGACY_TIME),
            None => DateTime::from_float_with_config(
                number.as_f64().unwrap_or_else(|| {
                    if number.as_str().starts_with('-') {
                        f64::NEG_INFINITY
                    } else {
                        f64::INFINITY
                    }
                }),
                &LEGACY_TIME,
            ),
        },
        _ => {
            return Some(FieldError::new(
                "datetime_type",
                "Input should be a valid datetime",
            ));
        }
    };
    match parsed {
        Ok(date) if date.time.tz_offset.is_none() => Some(timezone_aware()),
        Ok(date) if date.date.year == 0 => Some(datetime_parsing(
            "datetime_parsing",
            "datetime",
            "year 0 is out of range",
        )),
        Ok(_) => None,
        Err(error) => Some(datetime_parsing(
            "datetime_parsing",
            "datetime",
            parse_reason(&error),
        )),
    }
}

fn timezone_aware() -> FieldError {
    FieldError::new("timezone_aware", "Input should have timezone info")
}

fn parse_reason(error: &ParseError) -> &str {
    error.get_documentation().unwrap_or("invalid datetime")
}

fn datetime_parsing(kind: &'static str, expected: &str, reason: &str) -> FieldError {
    FieldError::new(
        kind,
        format!("Input should be a valid {expected}, {reason}"),
    )
    .context(json!({"error": reason}))
}
