use num_bigint::BigInt;
use serde_json::Value;

use super::JsonError;

/// Coerce the legacy typed integer argument while retaining large integer values.
pub fn integer_argument(value: &Value) -> Option<BigInt> {
    let parsed = match value {
        Value::Bool(value) => BigInt::from(u8::from(*value)),
        Value::Number(value) if !value.as_str().contains(['.', 'e', 'E']) => {
            bounded_integer_text(value.as_str())?
        }
        Value::Number(_) => {
            let value = number(value).ok()?;
            if value.fract() != 0.0 || value.abs() >= i64::MAX as f64 {
                return None;
            }
            bounded_integer_text(&format!("{value:.0}"))?
        }
        Value::String(value) => {
            let value = value.trim();
            let integral = match value.split_once('.') {
                Some((integral, fraction))
                    if !fraction.is_empty() && fraction.bytes().all(|byte| byte == b'0') =>
                {
                    integral
                }
                Some(_) => return None,
                None => value,
            };
            let digits = integral.strip_prefix(['+', '-']).unwrap_or(integral);
            if !digits.as_bytes().first().is_some_and(u8::is_ascii_digit)
                || !digits.as_bytes().last().is_some_and(u8::is_ascii_digit)
                || digits.contains("__")
            {
                return None;
            }
            bounded_integer_text(&integral.replace('_', ""))?
        }
        _ => return None,
    };
    Some(parsed)
}

fn bounded_integer_text(text: &str) -> Option<BigInt> {
    let significant = text.trim_start_matches(['+', '-']).trim_start_matches('0');
    let sign = usize::from(text.starts_with('-') && !significant.is_empty());
    // The pinned validator bounds the canonical signed decimal representation.
    if significant.len() + sign > 4300 {
        return None;
    }
    BigInt::parse_bytes(text.as_bytes(), 10)
}

/// Keep integer/float distinctions when multiplying archived numeric fields.
pub fn multiply_numbers(first: &Value, second: &Value) -> Result<Value, JsonError> {
    if let (Some(first), Some(second)) = (integer(first), integer(second)) {
        return integer_value(first * second);
    }
    finite_value(number(first)? * number(second)?)
}

/// Reproduce the application's Python sum over retained numeric evidence.
/// The floating accumulator follows CPython 3.15.2 Python/bltinmodule.c.
/// Its source notice is retained in timestamp/LICENSE-python.txt.
pub fn sum_numbers<'a>(values: impl IntoIterator<Item = &'a Value>) -> Result<Value, JsonError> {
    let mut exact = BigInt::from(0);
    let mut floating = None;
    let mut compensation = 0.0;
    let mut generic = false;
    for value in values {
        if floating.is_none()
            && let Some(value) = integer(value)
        {
            exact += value;
            generic |= i64::try_from(&exact).is_err();
            continue;
        }
        let previous = match floating {
            Some(value) => value,
            None => exact
                .to_string()
                .parse::<f64>()
                .map_err(|_| JsonError::NonfiniteNumber)?,
        };
        let value = number(value)?;
        let total = previous + value;
        if floating.is_none() || generic {
            floating = Some(total);
            continue;
        }
        compensation += if previous.abs() >= value.abs() {
            (previous - total) + value
        } else {
            (value - total) + previous
        };
        floating = Some(total);
    }
    match floating {
        None => integer_value(exact),
        Some(total) if compensation != 0.0 && compensation.is_finite() => {
            finite_value(total + compensation)
        }
        Some(total) => finite_value(total),
    }
}

fn integer(value: &Value) -> Option<BigInt> {
    match value {
        Value::Bool(value) => Some(BigInt::from(u8::from(*value))),
        Value::Number(number) if !number.as_str().contains(['.', 'e', 'E']) => {
            BigInt::parse_bytes(number.as_str().as_bytes(), 10)
        }
        _ => None,
    }
}

fn number(value: &Value) -> Result<f64, JsonError> {
    let number = match value {
        Value::Bool(value) => Some(f64::from(u8::from(*value))),
        value => value.as_f64(),
    };
    number
        .filter(|number| number.is_finite())
        .ok_or(JsonError::NonfiniteNumber)
}

fn integer_value(value: BigInt) -> Result<Value, JsonError> {
    serde_json::from_str(&value.to_string()).map_err(|_| JsonError::InvalidJson)
}

fn finite_value(value: f64) -> Result<Value, JsonError> {
    serde_json::Number::from_f64(value)
        .map(Value::Number)
        .ok_or(JsonError::NonfiniteNumber)
}
