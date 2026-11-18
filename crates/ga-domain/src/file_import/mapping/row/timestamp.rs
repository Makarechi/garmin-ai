use serde_json::Value;
use time::{Date, Duration, Month, Time, Weekday};

use crate::{event_input::source_offset_datetime, timestamp::StoredInstant};

pub(super) fn parse(value: Option<&Value>, timezone: &str) -> Result<StoredInstant, &'static str> {
    let text = value.and_then(Value::as_str).ok_or("timestamp_type")?;
    let (date, rest) = date(text).ok_or("timestamp_format")?;
    let Some(separator) = rest.chars().next() else {
        return Err("timestamp_offset_required");
    };
    let rest = &rest[separator.len_utf8()..];
    let offset_start = rest.find(['+', '-', 'Z']).unwrap_or(rest.len());
    let (clock, offset) = rest.split_at(offset_start);
    let (hour, minute, second, micros) = clock_parts(clock).ok_or("timestamp_format")?;
    let clock =
        Time::from_hms_micro(hour, minute, second, micros).map_err(|_| "timestamp_format")?;
    let offset = match offset {
        "" => return Err("timestamp_offset_required"),
        "Z" => 0,
        _ => utc_offset(offset).ok_or("timestamp_format")?,
    };
    let at = date
        .with_time(clock)
        .assume_utc()
        .checked_sub(Duration::microseconds(offset))
        .ok_or("timestamp_range")?;
    let at = StoredInstant::try_from(at).map_err(|_| "timestamp_range")?;
    let local = source_offset_datetime(at, timezone)
        .filter(|local| (1..=9999).contains(&local.year()))
        .ok_or("timestamp_range")?;
    if i64::from(local.offset().whole_seconds()) * 1_000_000 != offset {
        return Err("timestamp_timezone_mismatch");
    }
    Ok(at)
}

fn date(text: &str) -> Option<(Date, &str)> {
    let year: i32 = digits(text.get(..4)?)?;
    if year == 0 {
        return None;
    }
    let tail = text.get(4..)?;
    if let Some(week) = tail.strip_prefix("-W") {
        return week_date(year, week, true);
    }
    if let Some(week) = tail.strip_prefix('W') {
        return week_date(year, week, false);
    }
    let (month, day, length) = if tail.starts_with('-') && tail.get(3..4)? == "-" {
        (tail.get(1..3)?, tail.get(4..6)?, 6)
    } else {
        (tail.get(..2)?, tail.get(2..4)?, 4)
    };
    let day = Date::from_calendar_date(
        year,
        Month::try_from(digits::<u8>(month)?).ok()?,
        digits(day)?,
    )
    .ok()?;
    Some((day, tail.get(length..)?))
}

fn week_date(year: i32, text: &str, extended: bool) -> Option<(Date, &str)> {
    let week = digits(text.get(..2)?)?;
    let rest = text.get(2..)?;
    let day = if extended {
        rest.strip_prefix('-')
    } else {
        Some(rest)
    };
    let weekday = day.and_then(|day| digits::<u8>(day.get(..1)?));
    let (weekday, rest) = if let Some(weekday) = weekday {
        (weekday, day?.get(1..)?)
    } else {
        (1, rest)
    };
    let weekday = match weekday {
        1 => Weekday::Monday,
        2 => Weekday::Tuesday,
        3 => Weekday::Wednesday,
        4 => Weekday::Thursday,
        5 => Weekday::Friday,
        6 => Weekday::Saturday,
        7 => Weekday::Sunday,
        _ => return None,
    };
    Some((Date::from_iso_week_date(year, week, weekday).ok()?, rest))
}

fn digits<T: std::str::FromStr>(value: &str) -> Option<T> {
    (!value.is_empty() && value.bytes().all(|digit| digit.is_ascii_digit()))
        .then(|| value.parse().ok())
        .flatten()
}

fn clock_parts(text: &str) -> Option<(u8, u8, u8, u32)> {
    let (whole, fraction) = text.split_once(['.', ',']).unwrap_or((text, ""));
    if (text != whole && fraction.is_empty())
        || !fraction.bytes().all(|digit| digit.is_ascii_digit())
    {
        return None;
    }
    let hour = digits(whole.get(..2)?)?;
    let tail = whole.get(2..)?;
    let extended = tail.starts_with(':');
    let mut remainder = tail.strip_prefix(':').unwrap_or(tail);
    let (mut minute, mut second) = (0, 0);
    if !remainder.is_empty() || extended {
        minute = digits(remainder.get(..2)?)?;
        remainder = remainder.get(2..)?;
        if !remainder.is_empty() {
            if extended {
                remainder = remainder.strip_prefix(':')?;
            }
            if remainder.len() != 2 {
                return None;
            }
            second = digits(remainder)?;
        }
    }
    let micros = fraction
        .bytes()
        .chain(std::iter::repeat(b'0'))
        .take(6)
        .fold(0, |micros, digit| micros * 10 + u32::from(digit - b'0'));
    Some((hour, minute, second, micros))
}

fn utc_offset(text: &str) -> Option<i64> {
    let (sign, body) = text.split_at_checked(1)?;
    let (hour, minute, second, micros) = clock_parts(body)?;
    let seconds = i64::from(hour) * 3_600 + i64::from(minute) * 60 + i64::from(second);
    if seconds >= 86_400 {
        return None;
    }
    // Python treats a zero-hours/minutes/seconds offset as UTC even with a fraction.
    let offset = seconds * 1_000_000 + if seconds == 0 { 0 } else { i64::from(micros) };
    Some(if sign == "-" { -offset } else { offset })
}
