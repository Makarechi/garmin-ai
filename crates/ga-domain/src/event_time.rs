use serde_json::Value;
use speedate::{
    DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, ParseError, TimeConfig,
    TimestampUnit,
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

// These are the date and timestamp error messages exposed by the pinned
// Python validator. Keep the dependency on the existing date parser.
pub(crate) fn speedate_error_reason(error: &ParseError) -> &'static str {
    match error {
        ParseError::TooShort => "input is too short",
        ParseError::ExtraCharacters => "unexpected extra characters at the end of the input",
        ParseError::InvalidCharDateTimeSep => {
            "invalid datetime separator, expected `T`, `t`, `_` or space"
        }
        ParseError::InvalidCharDateSep => "invalid date separator, expected `-`",
        ParseError::DateNotExact => "Timestamp is not an exact date",
        ParseError::InvalidCharYear => "invalid character in year",
        ParseError::InvalidCharMonth => "invalid character in month",
        ParseError::InvalidCharDay => "invalid character in day",
        ParseError::InvalidCharTimeSep => "invalid time separator, expected `:`",
        ParseError::InvalidCharHour => "invalid character in hour",
        ParseError::InvalidCharMinute => "invalid character in minute",
        ParseError::InvalidCharSecond => "invalid character in second",
        ParseError::InvalidCharSecondFraction => "invalid character in second fraction",
        ParseError::InvalidCharTzSign => "invalid timezone sign",
        ParseError::InvalidCharTzHour => "invalid timezone hour",
        ParseError::InvalidCharTzMinute => "invalid timezone minute",
        ParseError::OutOfRangeTzMinute => "timezone minute value is outside expected range of 0-59",
        ParseError::OutOfRangeTz => "timezone offset must be less than 24 hours",
        ParseError::TzRequired => "timezone is required to adjust to a new timezone",
        ParseError::OutOfRangeMonth => "month value is outside expected range of 1-12",
        ParseError::OutOfRangeDay => "day value is outside expected range",
        ParseError::OutOfRangeHour => "hour value is outside expected range of 0-23",
        ParseError::OutOfRangeMinute => "minute value is outside expected range of 0-59",
        ParseError::OutOfRangeSecond => "second value is outside expected range of 0-59",
        ParseError::SecondFractionTooLong => "second fraction value is more than 6 digits long",
        ParseError::SecondFractionMissing => "second fraction digits missing after `.`",
        ParseError::MillisecondFractionTooLong => {
            "millisecond fraction value is more than 3 digits long"
        }
        ParseError::DateTooSmall => "dates before 0000 are not supported as unix timestamps",
        ParseError::DateTooLarge => "dates after 9999 are not supported as unix timestamps",
        _ => "invalid datetime",
    }
}
