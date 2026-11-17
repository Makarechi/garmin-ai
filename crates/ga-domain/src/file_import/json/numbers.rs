use std::str;

use jiter::NumberFloat;
use serde_json::Number;

use super::ImportFileError;
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
    if Decimal::new(token)? != Decimal::new(rounded)? {
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

#[derive(Eq, PartialEq)]
struct Decimal {
    negative: bool,
    significant: Vec<u8>,
    exponent: i64,
}

impl Decimal {
    fn new(token: &str) -> Result<Self, ImportFileError> {
        let negative = token.starts_with('-');
        let token = token.strip_prefix('-').unwrap_or(token);
        let (coefficient, exponent) = token.split_once(['e', 'E']).unwrap_or((token, "0"));
        let (integer, fraction) = coefficient.split_once('.').unwrap_or((coefficient, ""));
        let mut digits: Vec<_> = integer
            .bytes()
            .chain(fraction.bytes())
            .skip_while(|&digit| digit == b'0')
            .collect();
        let Some(last) = digits.iter().rposition(|&digit| digit != b'0') else {
            return Ok(Self {
                negative: false,
                significant: Vec::new(),
                exponent: 0,
            });
        };
        let exponent = exponent
            .parse::<i64>()
            .ok()
            .and_then(|value| value.checked_sub(fraction.len() as i64))
            .and_then(|value| value.checked_add((digits.len() - last - 1) as i64))
            .ok_or(ImportFileError::JsonPrecision)?;
        digits.truncate(last + 1);
        Ok(Self {
            negative,
            significant: digits,
            exponent,
        })
    }
}
