use serde::Serialize;
use serde_json::{Value, json};
use time::OffsetDateTime;

use crate::{
    FitError,
    profile::{Component, Field, FitProfile},
    value::RawFitValue::{self, Array, Bytes, Float, Invalid, Signed, Text, Unsigned},
};

use super::developer::DeveloperField;

const FIT_EPOCH_UNIX: i64 = 631_065_600;
const MIN_ABSOLUTE_TIMESTAMP: f64 = 268_435_456.0;

#[derive(Serialize)]
pub(super) struct DisplayField {
    pub name: Option<String>,
    #[serde(rename = "field_number")]
    pub number: u8,
    #[serde(rename = "developer_data_index")]
    pub developer_index: Option<u8>,
    native_field_number: Option<u8>,
    developer: bool,
    pub units: Option<String>,
    #[serde(rename = "type")]
    kind: String,
    pub value: Value,
    raw_value: Value,
}

impl DisplayField {
    pub(super) fn projected(
        field: &Field,
        raw: &RawFitValue,
        value: RawFitValue,
    ) -> Result<Self, FitError> {
        let (value, units) = process_type(&field.kind, value, field.units.clone())?;
        Ok(Self {
            name: Some(field.name.clone()),
            number: field.number,
            developer_index: None,
            native_field_number: None,
            developer: false,
            units,
            kind: field.kind.clone(),
            value,
            raw_value: json_value(raw),
        })
    }

    pub(super) fn unknown(number: u8, kind: &str, raw: &RawFitValue) -> Self {
        let value = json_value(raw);
        Self {
            name: Some(format!("unknown_{number}")),
            number,
            developer_index: None,
            native_field_number: None,
            developer: false,
            units: None,
            kind: kind.to_owned(),
            raw_value: value.clone(),
            value,
        }
    }

    pub(super) fn developer(
        number: u8,
        index: u8,
        base: &str,
        raw: &RawFitValue,
        found: Option<&DeveloperField>,
    ) -> Self {
        let value = json_value(raw);
        Self {
            name: found.and_then(|field| field.name.clone()),
            number,
            developer_index: Some(index),
            native_field_number: found.and_then(|field| field.native_field),
            developer: true,
            units: found.and_then(|field| field.units.clone()),
            kind: base.to_owned(),
            raw_value: value.clone(),
            value,
        }
    }

    pub(super) fn compressed_timestamp(raw: u64) -> Result<Self, FitError> {
        let (value, units) = process_type("date_time", Unsigned(raw), Some("s".to_owned()))?;
        Ok(Self {
            name: Some("timestamp".to_owned()),
            number: 253,
            developer_index: None,
            native_field_number: None,
            developer: false,
            units,
            kind: "date_time".to_owned(),
            value,
            raw_value: json!(raw),
        })
    }
}

pub(super) fn display(
    profile: &FitProfile,
    field: &Field,
    raw: &RawFitValue,
) -> Result<DisplayField, FitError> {
    let value = scale(label(profile, field, raw), field.scale, field.offset);
    DisplayField::projected(field, raw, value)
}

pub(super) fn label(profile: &FitProfile, field: &Field, raw: &RawFitValue) -> RawFitValue {
    // Enumeration applies to scalar fields. The pinned profile lookup compares
    // a whole array to enum keys and leaves its numeric elements intact.
    integer(raw)
        .and_then(|index| profile.types.get(&field.kind)?.values.get(&index))
        .map_or_else(|| raw.clone(), |label| Text(label.clone()))
}

pub(super) fn scale(value: RawFitValue, divisor: Option<f64>, offset: Option<i64>) -> RawFitValue {
    let divisor = divisor.filter(|divisor| *divisor != 0.0);
    let offset = offset.filter(|offset| *offset != 0);
    if divisor.is_none() && offset.is_none() {
        return value;
    }
    if let Array(items) = value {
        return Array(
            items
                .into_iter()
                .map(|item| scale(item, divisor, offset))
                .collect(),
        );
    }
    if let Bytes(bytes) = value {
        return Array(
            bytes
                .into_iter()
                .map(|byte| scale(Unsigned(u64::from(byte)), divisor, offset))
                .collect(),
        );
    }
    if let Some(divisor) = divisor
        && let Some(raw) = number(&value)
    {
        return Float(raw / divisor - offset.unwrap_or(0) as f64);
    }
    let Some(offset) = offset else {
        return value;
    };
    match value {
        Unsigned(raw) => signed_or_unsigned(i128::from(raw) - i128::from(offset)),
        Signed(raw) => signed_or_unsigned(i128::from(raw) - i128::from(offset)),
        Float(raw) => Float(raw - offset as f64),
        _ => value,
    }
}

fn signed_or_unsigned(value: i128) -> RawFitValue {
    if let Ok(value) = u64::try_from(value) {
        Unsigned(value)
    } else if let Ok(value) = i64::try_from(value) {
        Signed(value)
    } else {
        Float(value as f64)
    }
}

pub(super) fn integer(value: &RawFitValue) -> Option<i64> {
    match value {
        Signed(value) => Some(*value),
        Unsigned(value) => i64::try_from(*value).ok(),
        Float(value)
            if value.trunc() == *value
                && *value < (i64::MAX as f64)
                && *value >= (i64::MIN as f64) =>
        {
            Some(*value as i64)
        }
        _ => None,
    }
}

pub(super) fn unsigned(value: &RawFitValue) -> Option<u64> {
    match value {
        Unsigned(value) => Some(*value),
        Signed(value) => u64::try_from(*value).ok(),
        _ => None,
    }
}

pub(super) fn number(value: &RawFitValue) -> Option<f64> {
    match value {
        Unsigned(value) => Some(*value as f64),
        Signed(value) => Some(*value as f64),
        Float(value) => Some(*value),
        _ => None,
    }
}

pub(super) fn pack_component_value(
    value: &RawFitValue,
    component: &Component,
) -> Result<Option<RawFitValue>, FitError> {
    let offset = usize::from(component.bit_offset);
    let width = usize::from(component.bits);
    if width == 0 || width >= 64 {
        return Err(FitError::InvalidProfile);
    }
    let mask = (1_u64 << width) - 1;
    let packed = match value {
        Bytes(bytes) => read_bits(bytes, offset, width).map(Unsigned),
        Array(items) => {
            if offset != 0 && offset >= items.len() * 8 {
                return Ok(None);
            }
            let mut carry = 0_i128;
            let size = (offset + width).div_ceil(8);
            let mut bytes = Vec::with_capacity(size);
            for index in 0..size {
                let value = match items.get(index) {
                    Some(Signed(value)) => i128::from(*value),
                    Some(Unsigned(value)) => i128::from(*value),
                    None => 0,
                    _ => return Err(FitError::InvalidRecord),
                };
                let sum = carry + value;
                bytes.push(sum.rem_euclid(256) as u8);
                carry = sum.div_euclid(256);
            }
            read_bits(&bytes, offset, width).map(Unsigned)
        }
        Unsigned(raw) => Some(Unsigned(raw.checked_shr(offset as u32).unwrap_or(0) & mask)),
        Signed(raw) => {
            let shifted = raw
                .checked_shr(offset as u32)
                .unwrap_or(if *raw < 0 { -1 } else { 0 });
            Some(Unsigned(shifted as u64 & mask))
        }
        _ => Some(value.clone()),
    };
    Ok(packed)
}

fn read_bits(bytes: &[u8], offset: usize, width: usize) -> Option<u64> {
    if offset != 0 && offset >= bytes.len() * 8 {
        return None;
    }
    let end = (offset + width).min(bytes.len() * 8);
    let mut value = 0;
    for bit in offset..end {
        value |= u64::from((bytes[bit / 8] >> (bit % 8)) & 1) << (bit - offset);
    }
    Some(value)
}

fn json_value(value: &RawFitValue) -> Value {
    match value {
        Invalid => Value::Null,
        Unsigned(value) => json!(value),
        Signed(value) => json!(value),
        Float(value) => json!(value),
        Text(value) => json!(value),
        Bytes(value) => json!(value),
        Array(values) => Value::Array(values.iter().map(json_value).collect()),
    }
}

fn process_type(
    kind: &str,
    value: RawFitValue,
    units: Option<String>,
) -> Result<(Value, Option<String>), FitError> {
    if matches!(value, Invalid) {
        return Ok((Value::Null, units));
    }
    match kind {
        "bool" => {
            let truthy = match value {
                Signed(value) => value != 0,
                Unsigned(value) => value != 0,
                Float(value) => value != 0.0,
                Text(ref value) => !value.is_empty(),
                Bytes(ref value) => !value.is_empty(),
                Array(ref value) => !value.is_empty(),
                Invalid => false,
            };
            Ok((Value::Bool(truthy), units))
        }
        "date_time" | "local_date_time" => {
            let seconds = number(&value).ok_or(FitError::InvalidRecord)?;
            if kind == "date_time" && seconds < MIN_ABSOLUTE_TIMESTAMP {
                return Ok((json_value(&value), units));
            }
            Ok((timestamp(seconds)?, None))
        }
        "localtime_into_day" => {
            let seconds = unsigned(&value).ok_or(FitError::InvalidRecord)?;
            let formatted = if seconds >= 86_400 {
                "23:59:59.999999".to_owned()
            } else {
                format!(
                    "{:02}:{:02}:{:02}",
                    seconds / 3600,
                    seconds / 60 % 60,
                    seconds % 60
                )
            };
            Ok((Value::String(formatted), None))
        }
        _ => Ok((json_value(&value), units)),
    }
}

pub(super) fn timestamp(seconds: f64) -> Result<Value, FitError> {
    if !seconds.is_finite() {
        return Err(FitError::InvalidRecord);
    }
    let unix = seconds + FIT_EPOCH_UNIX as f64;
    let whole_seconds = unix.floor();
    let micros = ((unix - whole_seconds) * 1_000_000.0).round_ties_even() as i128;
    let nanos = (whole_seconds as i128)
        .checked_mul(1_000_000_000)
        .and_then(|seconds| seconds.checked_add(micros * 1_000))
        .ok_or(FitError::InvalidRecord)?;
    let moment =
        OffsetDateTime::from_unix_timestamp_nanos(nanos).map_err(|_| FitError::InvalidRecord)?;
    let mut formatted = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        moment.year(),
        u8::from(moment.month()),
        moment.day(),
        moment.hour(),
        moment.minute(),
        moment.second(),
    );
    if moment.microsecond() != 0 {
        use std::fmt::Write;
        write!(formatted, ".{:06}", moment.microsecond()).map_err(|_| FitError::InvalidRecord)?;
    }
    formatted.push_str("+00:00");
    Ok(Value::String(formatted))
}
