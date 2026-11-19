use time::{Date, Duration, Weekday};

use super::StoredInstant;

// Compatibility details follow CPython 3.15.2 Modules/_datetimemodule.c.
// This Rust adaptation returns a checked UTC instant for the aware cursor contract.
// Python's source license is retained in the accompanying LICENSE-python.txt.

/// The legacy cursor uses datetime.fromisoformat, including basic/week dates,
/// arbitrary one-character separators and fractional-second UTC offsets.
pub(super) fn parse(text: &str) -> Option<StoredInstant> {
    let characters: Vec<_> = text.chars().collect();
    let separator = date_length(&characters)?;
    let separator_byte = text.char_indices().nth(separator)?.0;
    let after_separator = separator_byte + characters[separator].len_utf8();
    let date = date(&text[..separator_byte])?;
    let clock = &text[after_separator..];
    let zone = clock.find(['+', '-', 'Z'])?;
    let local = clock_microseconds(&clock[..zone], true)?;
    let offset = if &clock[zone..] == "Z" || clock[zone..].starts_with("Z\0") {
        0
    } else {
        let sign = match clock.as_bytes()[zone] {
            b'+' => 1,
            b'-' => -1,
            _ => return None,
        };
        sign * clock_microseconds(&clock[zone + 1..], false)?
    };
    date.midnight()
        .assume_utc()
        .checked_add(Duration::microseconds(local - offset))?
        .try_into()
        .ok()
}

fn date_length(text: &[char]) -> Option<usize> {
    if text.len() < 8 || !text[..4].iter().all(char::is_ascii_digit) {
        return None;
    }
    match (text[4], text[5]) {
        ('-', 'W') => {
            if text.get(8) == Some(&'-') {
                if text.get(10).is_some_and(char::is_ascii_digit) {
                    Some(8)
                } else {
                    Some(10)
                }
            } else {
                Some(8)
            }
        }
        ('-', _) => Some(10),
        ('W', _) => {
            let digits = text[7..]
                .iter()
                .take_while(|value| value.is_ascii_digit())
                .count();
            let first_non_digit = 7 + digits;
            if first_non_digit < 9 {
                Some(first_non_digit)
            } else if first_non_digit.is_multiple_of(2) {
                Some(7)
            } else {
                Some(8)
            }
        }
        _ => Some(8),
    }
}

fn date(text: &str) -> Option<Date> {
    if !text.is_ascii() || !matches!(text.len(), 7 | 8 | 10) {
        return None;
    }
    let bytes = text.as_bytes();
    let year: i32 = text[..4].parse().ok()?;
    if !(1..=9999).contains(&year) {
        return None;
    }
    let extended = bytes[4] == b'-';
    let position = 4 + usize::from(extended);
    if bytes[position] == b'W' {
        let week = digits(text.get(position + 1..position + 3)?, 2)?;
        let suffix = text.get(position + 3..)?;
        let day = if suffix.is_empty() {
            1
        } else {
            let day = if extended {
                suffix.strip_prefix('-')?
            } else {
                suffix
            };
            digits(day, 1)?
        };
        let weekday = match day {
            1 => Weekday::Monday,
            2 => Weekday::Tuesday,
            3 => Weekday::Wednesday,
            4 => Weekday::Thursday,
            5 => Weekday::Friday,
            6 => Weekday::Saturday,
            7 => Weekday::Sunday,
            _ => return None,
        };
        Date::from_iso_week_date(year, week, weekday).ok()
    } else {
        let month = digits(text.get(position..position + 2)?, 2)?;
        let suffix = text.get(position + 2..)?;
        let day = if extended {
            suffix.strip_prefix('-')?
        } else {
            suffix
        };
        Date::from_calendar_date(year, month.try_into().ok()?, digits(day, 2)?).ok()
    }
}

fn digits(text: &str, length: usize) -> Option<u8> {
    if text.len() != length || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

fn clock_microseconds(text: &str, local_clock: bool) -> Option<i64> {
    let bytes = text.as_bytes();
    let mut components = [0_u8; 3];
    let mut position = 0;
    let mut separated = false;
    let mut fraction = None;
    for (index, component) in components.iter_mut().enumerate() {
        *component = digits(text.get(position..position + 2)?, 2)?;
        position += 2;
        let next = bytes.get(position).copied();
        position += 1;
        if index == 0 {
            separated = next == Some(b':');
        }
        if matches!(next, Some(b'.' | b',')) {
            if index != 2 || position >= bytes.len() {
                return None;
            }
            fraction = Some(&text[position..]);
            break;
        }
        if position >= bytes.len() {
            // The legacy C parser accepts one final clock character before an offset.
            if !local_clock && next.is_some() {
                return None;
            }
            break;
        }
        if separated && next == Some(b':') {
            if index == 2 {
                return None;
            }
        } else if !separated {
            position -= 1;
            if index == 2 {
                fraction = Some(&text[position..]);
            }
        } else {
            return None;
        }
    }
    let mut micros = 0;
    if let Some(fraction) = fraction {
        let significant = fraction.len().min(6);
        if significant == 0
            || !fraction.as_bytes()[..significant]
                .iter()
                .all(u8::is_ascii_digit)
        {
            return None;
        }
        if !local_clock
            && fraction.as_bytes()[significant..]
                .iter()
                .find(|byte| !byte.is_ascii_digit())
                .is_some_and(|byte| *byte != 0)
        {
            return None;
        }
        for index in 0..6 {
            micros = micros * 10
                + i64::from(fraction.as_bytes().get(index).map_or(0, |byte| byte - b'0'));
        }
    }
    let [hours, minutes, seconds] = components;
    if minutes >= 60 || seconds >= 60 || hours > 24 {
        return None;
    }
    if hours == 24 && (!local_clock || minutes != 0 || seconds != 0 || micros != 0) {
        return None;
    }
    Some(
        ((i64::from(hours) * 60 + i64::from(minutes)) * 60 + i64::from(seconds)) * 1_000_000
            + micros,
    )
}
