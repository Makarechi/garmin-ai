use std::collections::BTreeSet;

use serde_json::Value;

use super::{ExtractionError, clock, context, patterns::regex, unicode};
use crate::{
    event_payload::python_whitespace, event_schema::chat_form::casefold,
    file_import::decimal::Decimal, tracker_selection::alnum,
};

fn in_ranges(ch: char, ranges: &[(u32, u32)]) -> bool {
    let value = u32::from(ch);
    let index = ranges.partition_point(|&(_, end)| end < value);
    ranges.get(index).is_some_and(|&(start, _)| start <= value)
}
fn word(ch: char) -> bool {
    ch == '_' || alnum(ch)
}

pub(super) fn complete_number(
    source: &str,
    start: usize,
    end: usize,
    attached: &BTreeSet<usize>,
) -> bool {
    let before = source
        .get(..start)
        .and_then(|text| text.chars().next_back());
    let after = source.get(end..).and_then(|text| text.chars().next());
    if before.is_some_and(|ch| in_ranges(ch, unicode::MARK))
        || after.is_some_and(|ch| in_ranges(ch, unicode::MARK))
    {
        return false;
    }
    if before.is_some_and(|ch| alnum(ch) || "_.,+-−﹣－٫٬".contains(ch)) {
        return false;
    }
    if after.is_some_and(word)
        && (!attached.contains(&end) || after.is_some_and(|ch| in_ranges(ch, unicode::DIGIT)))
    {
        return false;
    }
    !after.is_some_and(|ch| {
        ".,٫٬".contains(ch)
            && source[end + ch.len_utf8()..]
                .chars()
                .next()
                .is_some_and(|ch| in_ranges(ch, unicode::DIGIT))
    })
}

fn supported_context(source: &str, start: usize, end: usize) -> Result<bool, ExtractionError> {
    Ok(!context::negated(source, start, end)? && !context::uncertain(source, start, end)?)
}

pub(super) fn folded_matches(value: &str, quote: &str, boundary: bool) -> Vec<(usize, usize)> {
    let wanted = casefold(value);
    if wanted.is_empty() {
        return Vec::new();
    }
    let mut folded = String::new();
    let mut offsets = Vec::new();
    for (start, ch) in quote.char_indices() {
        let part = casefold(&ch.to_string());
        for character in part.chars() {
            offsets.push((folded.len(), start, start + ch.len_utf8()));
            folded.push(character);
        }
    }
    let mut result = Vec::new();
    for (start, matched) in folded.match_indices(&wanted) {
        let end = start + matched.len();
        if boundary
            && (folded[..start].chars().next_back().is_some_and(word)
                || folded[end..].chars().next().is_some_and(word))
        {
            continue;
        }
        let first = offsets.partition_point(|&(byte, _, _)| byte < start);
        let last = offsets.partition_point(|&(byte, _, _)| byte < end);
        if let (Some(&(_, begin, _)), Some(&(_, _, stop))) = (
            offsets.get(first),
            last.checked_sub(1).and_then(|index| offsets.get(index)),
        ) && casefold(&quote[begin..stop]) == wanted
        {
            result.push((begin, stop));
        }
    }
    result
}

fn boolean_literal(value: &str) -> Option<bool> {
    match casefold(value).as_str() {
        "true" | "yes" | "да" | "есть" => Some(true),
        "false" | "no" | "нет" => Some(false),
        _ => None,
    }
}

fn boolean(value: bool, quote: &str, source: &str, offset: usize) -> Result<bool, ExtractionError> {
    let normalized = casefold(quote);
    let words = regex("words")?
        .find_iter(&normalized)
        .map(|matched| matched.map(|value| value.as_str()))
        .collect::<Result<Vec<_>, _>>()?;
    if words.iter().enumerate().any(|(index, word)| {
        boolean_literal(word).is_some()
            && index > 0
            && ["not", "не", "no"].contains(&words[index - 1])
    }) {
        return Ok(false);
    }
    let positive = words
        .iter()
        .any(|word| ["true", "yes", "да", "есть"].contains(word));
    let negative = words
        .iter()
        .any(|word| ["false", "no", "нет"].contains(word))
        || normalized.contains("не было");
    if !(if value {
        positive && !negative
    } else {
        negative && !positive
    }) {
        return Ok(false);
    }
    for matched in regex(if value { "positive" } else { "negative" })?.find_iter(quote) {
        let matched = matched?;
        let start = offset + matched.start();
        let end = offset + matched.end();
        if source[..start].chars().next_back().is_some_and(word)
            || source[end..].chars().next().is_some_and(word)
        {
            continue;
        }
        if supported_context(source, start, end)? {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(super) fn value(
    value: &Value,
    quote: &str,
    source: &str,
    offset: usize,
    semantic: Option<&str>,
    attached: &BTreeSet<usize>,
) -> Result<bool, ExtractionError> {
    if let Some(value) = value.as_bool() {
        return boolean(value, quote, source, offset);
    }
    if let Some(text) = value.as_str()
        && let Some(expected) = boolean_literal(text)
    {
        if !boolean(expected, quote, source, offset)? {
            return Ok(false);
        }
        for (start, end) in folded_matches(text, quote, true) {
            if boolean(expected, &quote[start..end], source, offset + start)? {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    if value.is_number() {
        let expected = numeric_decimal(value)?;
        let temporal = clock::temporal_spans(source)?;
        for matched in regex("number")?.find_iter(quote) {
            let matched = matched?;
            let start = offset + matched.start();
            let end = offset + matched.end();
            if temporal
                .iter()
                .any(|&(begin, stop)| start < stop && end > begin)
                || !complete_number(source, start, end, attached)
            {
                continue;
            }
            let token = normalize_decimal(matched.as_str()).ok_or(ExtractionError::Invalid)?;
            if Decimal::new(&token).as_ref() == Some(&expected)
                && supported_context(source, start, end)?
            {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    let Some(text) = value
        .as_str()
        .filter(|text| !text.trim_matches(python_whitespace).is_empty())
    else {
        return Ok(false);
    };
    if regex("whole_number")?.is_match(text)? {
        let temporal = clock::temporal_spans(source)?;
        for (start, matched) in quote.match_indices(text) {
            let start = offset + start;
            let end = start + matched.len();
            if complete_number(source, start, end, attached)
                && !temporal
                    .iter()
                    .any(|&(begin, stop)| start < stop && end > begin)
                && supported_context(source, start, end)?
            {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    for (start, end) in folded_matches(text, quote, semantic != Some("text")) {
        let start = offset + start;
        let end = offset + end;
        if semantic != Some("text")
            && (source[..start].chars().next_back().is_some_and(word)
                || source[end..].chars().next().is_some_and(word))
        {
            continue;
        }
        if supported_context(source, start, end)? {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(super) fn normalize_decimal(text: &str) -> Option<String> {
    text.chars()
        .map(|ch| match ch {
            ',' => Some('.'),
            '-' | '+' | '.' | 'e' | 'E' => Some(ch),
            ch => crate::metric_contract::decimal_value(ch).map(|value| char::from(b'0' + value)),
        })
        .collect()
}

pub(super) fn numeric_decimal(value: &Value) -> Result<Decimal, ExtractionError> {
    let raw = value.as_number().ok_or(ExtractionError::Invalid)?.as_str();
    let text = if raw.contains(['.', 'e', 'E']) {
        let value = value
            .as_f64()
            .filter(|value| value.is_finite())
            .ok_or(ExtractionError::Invalid)?;
        let mut bytes = Vec::new();
        crate::legacy_json::write_float(&mut bytes, value).map_err(|_| ExtractionError::Invalid)?;
        String::from_utf8(bytes).map_err(|_| ExtractionError::Invalid)?
    } else {
        raw.to_owned()
    };
    Decimal::new(&text).ok_or(ExtractionError::Invalid)
}

pub(super) fn categorical(
    value: &Value,
    quote: &str,
    source: &str,
    offset: usize,
) -> Result<bool, ExtractionError> {
    if let Some(text) = value.as_str()
        && (boolean_literal(text).is_some() || regex("whole_number")?.is_match(text)?)
    {
        return self::value(value, quote, source, offset, None, &BTreeSet::new());
    }
    let text = match value {
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        _ => value.to_string(),
    };
    if regex("whole_number")?.is_match(&text)? {
        return self::value(
            &Value::String(text),
            quote,
            source,
            offset,
            None,
            &BTreeSet::new(),
        );
    }
    let expected = casefold(&text);
    let expected = regex("words")?
        .find_iter(&expected)
        .map(|matched| matched.map(|value| value.as_str().to_owned()))
        .collect::<Result<Vec<_>, _>>()?;
    if expected.is_empty() {
        return Ok(false);
    }
    let mut observed = Vec::new();
    for matched in regex("words")?.find_iter(quote) {
        let matched = matched?;
        let folded = casefold(matched.as_str());
        for token in regex("words")?.find_iter(&folded) {
            observed.push((token?.as_str().to_owned(), matched.start(), matched.end()));
        }
    }
    let temporal = clock::temporal_spans(source)?;
    for matched in observed.windows(expected.len()) {
        if !matched
            .iter()
            .map(|(value, _, _)| value)
            .eq(expected.iter())
        {
            continue;
        }
        let start = offset + matched[0].1;
        let end = offset + matched[matched.len() - 1].2;
        if !supported_context(source, start, end)? {
            continue;
        }
        let mut borrowed_clock = false;
        for (token, start, end) in matched {
            let decimal = token
                .chars()
                .all(|ch| crate::metric_contract::decimal_value(ch).is_some());
            borrowed_clock |= decimal
                && temporal
                    .iter()
                    .any(|&(begin, stop)| offset + start < stop && offset + end > begin);
        }
        if borrowed_clock {
            continue;
        }
        if matched.len() == 1
            && matched[0]
                .0
                .chars()
                .all(|ch| crate::metric_contract::decimal_value(ch).is_some())
            && !complete_number(source, start, end, &BTreeSet::new())
        {
            continue;
        }
        return Ok(true);
    }
    Ok(false)
}

pub(super) fn unit_spans(
    unit: Option<&str>,
    quote: &str,
    source: &str,
    offset: usize,
) -> Result<Vec<(usize, usize)>, ExtractionError> {
    let Some(unit) = unit.filter(|unit| !unit.is_empty()) else {
        return Ok(Vec::new());
    };
    let aliases: &[&str] = match unit {
        "minutes" => &[
            "minute",
            "minutes",
            "min",
            "минута",
            "минуты",
            "минут",
            "мин",
        ],
        "hours" => &["hour", "hours", "h", "час", "часа", "часов"],
        "seconds" => &[
            "second",
            "seconds",
            "sec",
            "s",
            "секунда",
            "секунды",
            "секунд",
        ],
        _ => &[unit],
    };
    let mut spans = Vec::new();
    for alias in aliases {
        for (start, end) in folded_matches(alias, quote, false) {
            let start = offset + start;
            let end = offset + end;
            let before = source[..start].chars().next_back();
            let after = source[end..].chars().next();
            if before.is_some_and(|ch| in_ranges(ch, unicode::MARK))
                || after.is_some_and(|ch| in_ranges(ch, unicode::MARK))
            {
                continue;
            }
            if before.is_some_and(|ch| crate::metric_contract::decimal_value(ch).is_some()) {
                let number = regex("number_suffix")?.find(&source[..start])?;
                let Some(number) = number else { continue };
                if source[..number.start()].ends_with(':')
                    || !complete_number(
                        source,
                        number.start(),
                        start,
                        &[start].into_iter().collect(),
                    )
                {
                    continue;
                }
            }
            if alias.chars().next().is_some_and(word)
                && before.is_some_and(|ch| {
                    (alnum(ch) && crate::metric_contract::decimal_value(ch).is_none())
                        || "_/".contains(ch)
                })
            {
                continue;
            }
            if after.is_some_and(|ch| word(ch) || ch == '/') {
                continue;
            }
            spans.push((start, end));
        }
    }
    Ok(spans)
}
