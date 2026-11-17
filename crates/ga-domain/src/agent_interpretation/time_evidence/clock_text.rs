use time::OffsetDateTime;

use crate::metric_contract::decimal_value;

pub(super) fn has_explicit_offset(text: &str, at: OffsetDateTime) -> bool {
    let seconds = at.offset().whole_seconds();
    if seconds % 60 != 0 {
        return false;
    }
    let magnitude = seconds.unsigned_abs() / 60;
    let sign = if seconds < 0 { '-' } else { '+' };
    let plain = format!("{sign}{:02}{:02}", magnitude / 60, magnitude % 60);
    let colon = format!("{sign}{:02}:{:02}", magnitude / 60, magnitude % 60);

    let mut previous = None;
    let mut next_search = 0;
    let mut found = false;
    for (index, character) in text.char_indices() {
        let preceded_by_number = previous.is_some_and(|previous| {
            matches!(previous, ':' | '+' | '-') || decimal_value(previous).is_some()
        });
        previous = Some(character);
        if index < next_search || preceded_by_number || decimal_value(character).is_none() {
            continue;
        }
        let Some(mention) = parse_mention(&text[index..]) else {
            continue;
        };
        next_search = index + mention.length;
        if (mention.hour, mention.minute) == (at.hour(), at.minute()) {
            if !matches!(mention.offset, Some(offset) if offset == plain || offset == colon) {
                return false;
            }
            found = true;
        }
    }
    found
}

struct ClockMention<'a> {
    hour: u8,
    minute: u8,
    offset: Option<&'a str>,
    length: usize,
}

fn parse_mention(text: &str) -> Option<ClockMention<'_>> {
    let (hour, rest) = parse_hour(text)?;
    let (minute, rest) = minute_or_second(rest.strip_prefix(':')?)?;
    let after_seconds = rest
        .strip_prefix(':')
        .and_then(minute_or_second)
        .map_or(rest, |(_, rest)| consume_fraction(rest));
    let rest = after_seconds.trim_start_matches(clock_space);
    let rest = match rest.get(..3) {
        Some(prefix)
            if prefix.eq_ignore_ascii_case("UTC") || prefix.eq_ignore_ascii_case("GMT") =>
        {
            rest[3..].trim_start_matches(clock_space)
        }
        _ => rest,
    };
    let offset = parse_offset(rest);
    Some(ClockMention {
        hour,
        minute,
        offset,
        length: text.len() - rest.len() + offset.map_or(0, str::len),
    })
}

fn parse_hour(text: &str) -> Option<(u8, &str)> {
    let (first, rest) = digit(text)?;
    match text.as_bytes()[0] {
        b'0' | b'1' => {
            if let Some((second, rest)) = digit(rest) {
                return Some((first * 10 + second, rest));
            }
        }
        b'2' if rest
            .as_bytes()
            .first()
            .is_some_and(|second| matches!(second, b'0'..=b'3')) =>
        {
            return Some((20 + rest.as_bytes()[0] - b'0', &rest[1..]));
        }
        _ => {}
    }
    Some((first, rest))
}

fn minute_or_second(text: &str) -> Option<(u8, &str)> {
    let first = *text.as_bytes().first()?;
    if !matches!(first, b'0'..=b'5') {
        return None;
    }
    let (second, rest) = digit(&text[1..])?;
    Some(((first - b'0') * 10 + second, rest))
}

fn consume_fraction(rest: &str) -> &str {
    let Some(fraction) = rest.strip_prefix('.') else {
        return rest;
    };
    let end = fraction.trim_start_matches(|character| decimal_value(character).is_some());
    if end.len() == fraction.len() {
        rest
    } else {
        end
    }
}

fn parse_offset(text: &str) -> Option<&str> {
    let rest = text.strip_prefix(['+', '-'])?;
    let (_, rest) = digit(rest)?;
    let (_, rest) = digit(rest)?;
    let rest = rest.strip_prefix(':').unwrap_or(rest);
    let (_, rest) = digit(rest)?;
    let (_, rest) = digit(rest)?;
    Some(&text[..text.len() - rest.len()])
}

fn digit(text: &str) -> Option<(u8, &str)> {
    let character = text.chars().next()?;
    Some((decimal_value(character)?, &text[character.len_utf8()..]))
}

fn clock_space(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}
