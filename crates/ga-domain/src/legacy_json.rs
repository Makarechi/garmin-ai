use std::{collections::BTreeMap, error::Error, fmt, fmt::Write};

use jiter::JsonValue;

mod numbers;
pub use numbers::{integer_argument, multiply_numbers, sum_numbers};

/// Match the Python API boolean argument conversion.
#[must_use]
pub fn boolean_argument(value: &serde_json::Value) -> Option<bool> {
    match value {
        serde_json::Value::Bool(value) => Some(*value),
        serde_json::Value::Number(_) => match value.as_f64()? {
            0.0 => Some(false),
            1.0 => Some(true),
            _ => None,
        },
        serde_json::Value::String(value) => match value.to_ascii_lowercase().as_str() {
            "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
            "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
            _ => None,
        },
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Utf8Layout {
    /// The archive contract uses the default Python separator spacing.
    Archive,
    /// Application prompts keep insertion order and Python's default spacing.
    Ordered,
    /// Source record and file import confirmation hashes use compact JSON.
    Compact,
}

impl Utf8Layout {
    fn separator(self, out: &mut Vec<u8>, separator: u8) {
        out.push(separator);
        if self != Self::Compact {
            out.push(b' ');
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum JsonError {
    InputTooLarge,
    InvalidJson,
    NonfiniteNumber,
}

impl fmt::Display for JsonError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InputTooLarge => "JSON input exceeds the byte limit",
            Self::InvalidJson => "JSON input is invalid",
            Self::NonfiniteNumber => "JSON numbers must be finite",
        })
    }
}

impl Error for JsonError {}

/// Produce the UTF-8 representation used by the selected Python contract.
/// The caller supplies the size limit of the operation that owns the input.
pub fn encode(input: &[u8], layout: Utf8Layout, max_bytes: usize) -> Result<Vec<u8>, JsonError> {
    if input.len() > max_bytes {
        return Err(JsonError::InputTooLarge);
    }
    let value = JsonValue::parse(input, true).map_err(|_| JsonError::InvalidJson)?;
    let mut out = Vec::with_capacity(input.len());
    write_value(&mut out, &value, layout)?;
    Ok(out)
}

/// Match Python's `ensure_ascii` option for protocols that hash escaped text.
pub fn encode_ascii(
    input: &[u8],
    layout: Utf8Layout,
    max_bytes: usize,
) -> Result<String, JsonError> {
    let encoded = encode(input, layout, max_bytes)?;
    let utf8 = str::from_utf8(&encoded).map_err(|_| JsonError::InvalidJson)?;
    let mut ascii = String::with_capacity(utf8.len());
    for character in utf8.chars() {
        if character < '\u{007f}' {
            ascii.push(character);
        } else {
            for unit in character.encode_utf16(&mut [0; 2]) {
                write!(ascii, "\\u{unit:04x}").map_err(|_| JsonError::InvalidJson)?;
            }
        }
    }
    Ok(ascii)
}

pub(crate) fn write_value(
    out: &mut Vec<u8>,
    value: &JsonValue<'_>,
    layout: Utf8Layout,
) -> Result<(), JsonError> {
    match value {
        JsonValue::Null => out.extend_from_slice(b"null"),
        JsonValue::Bool(true) => out.extend_from_slice(b"true"),
        JsonValue::Bool(false) => out.extend_from_slice(b"false"),
        JsonValue::Int(value) => out.extend_from_slice(value.to_string().as_bytes()),
        JsonValue::BigInt(value) => out.extend_from_slice(value.to_string().as_bytes()),
        JsonValue::Float(value) => write_float(out, *value)?,
        JsonValue::Str(value) => write_string(out, value)?,
        JsonValue::Array(values) => {
            out.push(b'[');
            for (index, item) in values.iter().enumerate() {
                if index != 0 {
                    layout.separator(out, b',');
                }
                write_value(out, item, layout)?;
            }
            out.push(b']');
        }
        JsonValue::Object(values) => {
            if layout == Utf8Layout::Ordered {
                write_object(
                    out,
                    values.iter().map(|(key, value)| (key.as_ref(), value)),
                    layout,
                )?;
            } else {
                // Collecting in input order keeps the last copy of a repeated key,
                // as Python's JSON parser does. UTF-8 order is Unicode scalar order.
                let sorted: BTreeMap<_, _> = values
                    .iter()
                    .map(|(key, value)| (key.as_ref(), value))
                    .collect();
                write_object(out, sorted.into_iter(), layout)?;
            }
        }
    }
    Ok(())
}

fn write_object<'a>(
    out: &mut Vec<u8>,
    members: impl Iterator<Item = (&'a str, &'a JsonValue<'a>)>,
    layout: Utf8Layout,
) -> Result<(), JsonError> {
    out.push(b'{');
    for (index, (key, item)) in members.enumerate() {
        if index != 0 {
            layout.separator(out, b',');
        }
        write_string(out, key)?;
        layout.separator(out, b':');
        write_value(out, item, layout)?;
    }
    out.push(b'}');
    Ok(())
}

fn write_string(out: &mut Vec<u8>, value: &str) -> Result<(), JsonError> {
    serde_json::to_writer(out, value).map_err(|_| JsonError::InvalidJson)
}

pub(crate) fn write_float(out: &mut Vec<u8>, value: f64) -> Result<(), JsonError> {
    if !value.is_finite() {
        return Err(JsonError::NonfiniteNumber);
    }
    let mut buffer = zmij::Buffer::new();
    let text = buffer.format_finite(value);
    // Python uses scientific notation at 10^-5. The shortest digits agree;
    // this is the sole fixed-notation boundary that differs from Żmij's.
    let magnitude = text.strip_prefix('-').unwrap_or(text);
    if let Some(digits) = magnitude.strip_prefix("0.0000") {
        if value.is_sign_negative() {
            out.push(b'-');
        }
        let (first, tail) = digits
            .as_bytes()
            .split_first()
            .ok_or(JsonError::InvalidJson)?;
        out.push(*first);
        if !tail.is_empty() {
            out.push(b'.');
            out.extend_from_slice(tail);
        }
        out.extend_from_slice(b"e-05");
    } else if let Some((mantissa, exponent)) = text.split_once('e') {
        out.extend_from_slice(mantissa.as_bytes());
        out.push(b'e');
        let (sign, digits) = exponent
            .as_bytes()
            .split_first()
            .ok_or(JsonError::InvalidJson)?;
        out.push(*sign);
        if digits.len() == 1 {
            out.push(b'0');
        }
        out.extend_from_slice(digits);
    } else {
        out.extend_from_slice(text.as_bytes());
    }
    Ok(())
}
