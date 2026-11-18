use std::str;

use jiter::NumberFloat;
use serde_json::Number;

use super::{super::decimal::Decimal, ImportFileError};
use crate::legacy_json::write_float;

pub(super) fn parse(bytes: &[u8]) -> Result<Number, ImportFileError> {
    let token = str::from_utf8(bytes).map_err(|_| ImportFileError::InvalidFormat)?;
    if matches!(token, "NaN" | "Infinity" | "-Infinity") {
        return Err(ImportFileError::JsonNonfinite);
    }
    if token == "-0" {
        return Ok(Number::from(0));
    }
    if !token.contains(['.', 'e', 'E']) {
        return token.parse().map_err(|_| ImportFileError::InvalidFormat);
    }

    let parsed = token
        .parse::<f64>()
        .map_err(|_| ImportFileError::InvalidFormat)?;
    if !parsed.is_finite() {
        return Err(ImportFileError::JsonFloatOverflow);
    }
    let mut rounded = Vec::with_capacity(32);
    write_float(&mut rounded, parsed).map_err(|_| ImportFileError::JsonFloatOverflow)?;
    let rounded = str::from_utf8(&rounded).map_err(|_| ImportFileError::InvalidFormat)?;
    if Decimal::new(token).ok_or(ImportFileError::JsonPrecision)?
        != Decimal::new(rounded).ok_or(ImportFileError::JsonPrecision)?
    {
        return Err(ImportFileError::JsonPrecision);
    }
    rounded.parse().map_err(|_| ImportFileError::InvalidFormat)
}

pub(super) fn long_float(input: &[u8]) -> Result<&[u8], ImportFileError> {
    let len = input
        .iter()
        .position(|byte| !matches!(byte, b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-'))
        .unwrap_or(input.len());
    let token = &input[..len];
    if !token.iter().any(|byte| matches!(byte, b'.' | b'e' | b'E'))
        || NumberFloat::from_bytes(token, false).is_err()
    {
        return Err(ImportFileError::InvalidFormat);
    }
    Ok(token)
}
