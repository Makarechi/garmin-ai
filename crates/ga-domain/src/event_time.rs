use serde_json::Value;
use speedate::{
    DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig, TimestampUnit,
};

use crate::timestamp::StoredInstant;

// Match the parser and configuration used by the pinned Python event model.
pub(crate) const LEGACY_TIME: DateTimeConfig = DateTimeConfig {
    timestamp_unit: TimestampUnit::Infer,
    time_config: TimeConfig {
        microseconds_precision_overflow_behavior: MicrosecondsPrecisionOverflowBehavior::Truncate,
        unix_timestamp_offset: Some(0),
    },
};

pub(crate) fn aware(value: &Value) -> Option<StoredInstant> {
    as_stored_instant(parse_aware_datetime(value)?)
}

pub(crate) fn parse_aware_datetime(value: &Value) -> Option<DateTime> {
    let parsed = match value {
        Value::String(text) => DateTime::parse_bytes_with_config(text.as_bytes(), &LEGACY_TIME),
        Value::Number(number) => match number.as_i64() {
            Some(integer) => DateTime::from_timestamp_with_config(integer, 0, &LEGACY_TIME),
            None => DateTime::from_float_with_config(number.as_f64()?, &LEGACY_TIME),
        },
        _ => return None,
    }
    .ok()?;
    if parsed.date.year == 0 || parsed.time.tz_offset.is_none() {
        return None;
    }
    Some(parsed)
}

pub(crate) fn as_stored_instant(parsed: DateTime) -> Option<StoredInstant> {
    let microseconds = parsed.timestamp_tz() * 1_000_000 + i64::from(parsed.time.microsecond);
    StoredInstant::from_unix_microseconds(microseconds).ok()
}
