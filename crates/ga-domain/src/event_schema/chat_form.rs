//! Deterministic answers for the recorded Telegram tracker form contract.

mod advance;
mod presentation;
mod unicode;
mod values;

pub use advance::{ChatFormProgress, advance_chat_form, restart_chat_form_fields};
pub use presentation::{ChatFormStart, begin_chat_form, chat_form_prompt};
pub use values::{chat_choice_labels, parse_chat_form_value};

use std::{error::Error, fmt};

#[cfg(test)]
use serde_json::Value;
use time::{OffsetDateTime, UtcOffset};

#[cfg(test)]
use crate::timestamp::OffsetTimestamp;
use crate::timestamp::{StoredInstant, python_timezone};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ChatFormError {
    Invalid,
    DecimalRange,
    Answer(String),
}

impl fmt::Display for ChatFormError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid => formatter.write_str("Could not parse that answer"),
            Self::DecimalRange => {
                formatter.write_str("Decimal exponent is outside the supported range")
            }
            Self::Answer(message) => formatter.write_str(message),
        }
    }
}
impl Error for ChatFormError {}

fn russian(locale: &str) -> bool {
    locale
        .split('-')
        .next()
        .is_some_and(|code| code.eq_ignore_ascii_case("ru"))
}

fn message<'a>(locale: &str, ru: &'a str, en: &'a str) -> &'a str {
    if russian(locale) { ru } else { en }
}

fn answer(locale: &str, ru: &str, en: &str) -> ChatFormError {
    ChatFormError::Answer(message(locale, ru, en).to_owned())
}

pub(crate) fn casefold(text: &str) -> String {
    let mut folded = String::with_capacity(text.len());
    for character in text.chars() {
        match unicode::CASEFOLD.binary_search_by_key(&u32::from(character), |&(code, _)| code) {
            Ok(index) => folded.push_str(unicode::CASEFOLD[index].1),
            Err(_) => folded.push(character),
        }
    }
    folded
}

/// Parse an explicit local clock and refuse ambiguous, nonexistent or future times.
pub fn parse_chat_form_time(
    text: &str,
    timezone: &str,
    now: OffsetDateTime,
    locale: &str,
) -> Result<OffsetDateTime, ChatFormError> {
    parse_time(text, now, locale, || python_timezone(timezone))
}

fn parse_time(
    text: &str,
    now: OffsetDateTime,
    locale: &str,
    timezone: impl FnOnce() -> Option<jiff::tz::TimeZone>,
) -> Result<OffsetDateTime, ChatFormError> {
    if matches!(casefold(text).as_str(), "now" | "сейчас") {
        return Ok(now);
    }
    let characters: Vec<_> = text.chars().collect();
    // Python's regex accepts Unicode decimal digits, while fromisoformat requires ASCII.
    let valid_length = characters.len() == 16
        || characters.len() == 17 && characters[16] == 'Z'
        || characters.len() == 22 && matches!(characters[16], '+' | '-');
    let shape = valid_length
        && characters
            .iter()
            .enumerate()
            .all(|(index, &character)| match index {
                4 | 7 => character == '-',
                10 => matches!(character, ' ' | 'T'),
                13 | 19 => character == ':',
                16 => matches!(character, 'Z' | '+' | '-'),
                _ => crate::metric_contract::decimal_value(character).is_some(),
            });
    if !shape {
        return Err(answer(
            locale,
            "Укажите YYYY-MM-DD HH:MM и смещение, если оно требуется",
            "Use YYYY-MM-DD HH:MM, with an offset when required",
        ));
    }
    let invalid = || {
        answer(
            locale,
            "Некорректная дата или время",
            "Invalid calendar time",
        )
    };
    if !text.is_ascii() {
        return Err(invalid());
    }
    let civil: jiff::civil::DateTime = format!("{}T{}:00", &text[..10], &text[11..16])
        .parse()
        .map_err(|_| invalid())?;
    if !(1..=9999).contains(&civil.year()) {
        return Err(invalid());
    }
    let zone = timezone().ok_or(ChatFormError::Invalid)?;
    let date = time::Date::from_calendar_date(
        i32::from(civil.year()),
        time::Month::try_from(u8::try_from(civil.month()).map_err(|_| invalid())?)
            .map_err(|_| invalid())?,
        civil.day().try_into().map_err(|_| invalid())?,
    )
    .map_err(|_| invalid())?;
    let clock = time::Time::from_hms(
        civil.hour().try_into().map_err(|_| invalid())?,
        civil.minute().try_into().map_err(|_| invalid())?,
        0,
    )
    .map_err(|_| invalid())?;
    let offset = if characters.len() == 16 {
        match zone.to_ambiguous_timestamp(civil).offset() {
            jiff::tz::AmbiguousOffset::Unambiguous { offset } => {
                UtcOffset::from_whole_seconds(offset.seconds())
                    .map_err(|_| ChatFormError::Invalid)?
            }
            _ => {
                return Err(answer(
                    locale,
                    "Неоднозначное или несуществующее местное время; укажите UTC-смещение",
                    "Ambiguous or nonexistent local time; include a UTC offset",
                ));
            }
        }
    } else if characters.len() == 17 {
        UtcOffset::UTC
    } else {
        let hour = text[17..19].parse::<i32>().map_err(|_| invalid())?;
        let minute = text[20..22].parse::<i32>().map_err(|_| invalid())?;
        if minute > 59 {
            return Err(invalid());
        }
        let sign = if characters[16] == '-' { -1 } else { 1 };
        UtcOffset::from_whole_seconds(sign * (hour * 3600 + minute * 60)).map_err(|_| invalid())?
    };
    let result = time::PrimitiveDateTime::new(date, clock).assume_offset(offset);
    let instant = StoredInstant::try_from(result).map_err(|_| ChatFormError::Invalid)?;
    if characters.len() != 16 {
        let actual = timezone_offset(&zone, instant)?;
        OffsetDateTime::from(instant)
            .checked_to_offset(actual)
            .ok_or(ChatFormError::Invalid)?;
        if actual != offset {
            return Err(answer(
                locale,
                "UTC-смещение не совпадает с настроенным часовым поясом",
                "UTC offset does not match the configured timezone",
            ));
        }
    }
    if result > now + time::Duration::minutes(5) {
        return Err(answer(
            locale,
            "Время не может быть в будущем",
            "Time cannot be in the future",
        ));
    }
    Ok(result)
}

pub(crate) fn timezone_offset(
    zone: &jiff::tz::TimeZone,
    instant: StoredInstant,
) -> Result<UtcOffset, ChatFormError> {
    let timestamp =
        if let Ok(timestamp) = jiff::Timestamp::from_microsecond(instant.unix_microseconds()) {
            timestamp
        } else {
            // The Gregorian POSIX tail repeats every 400 years. Use the previous
            // cycle for the final UTC day beyond Jiff's offset-safe timestamp window.
            let at = OffsetDateTime::from(instant);
            let previous = at
                .replace_year(at.year() - 400)
                .map_err(|_| ChatFormError::Invalid)?;
            jiff::Timestamp::from_microsecond(
                StoredInstant::try_from(previous)
                    .map_err(|_| ChatFormError::Invalid)?
                    .unix_microseconds(),
            )
            .map_err(|_| ChatFormError::Invalid)?
        };
    UtcOffset::from_whole_seconds(zone.to_offset(timestamp).seconds())
        .map_err(|_| ChatFormError::Invalid)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn outcome(result: Result<Value, ChatFormError>) -> Value {
        match result {
            Ok(value) => serde_json::json!({"value":value}),
            Err(ChatFormError::Answer(error)) => serde_json::json!({"answer_error":error}),
            Err(ChatFormError::Invalid) => serde_json::json!({"invalid":true}),
            Err(ChatFormError::DecimalRange) => {
                serde_json::json!({"runtime_error":"InvalidOperation"})
            }
        }
    }

    #[test]
    fn matches_frozen_answers_and_clock_refusals() -> Result<(), Box<dyn Error>> {
        let cases: Value = serde_json::from_str(include_str!(
            "../../contracts/tracker-chat-form-values.json"
        ))?;
        for case in cases["values"].as_array().ok_or("Expected answers")? {
            let actual = parse_chat_form_value(
                &case["field"],
                case["answer"].as_str().ok_or("Expected answer")?,
                case["locale"].as_str().ok_or("Expected locale")?,
            );
            assert_eq!(outcome(actual), case["outcome"], "{case}");
        }
        for case in cases["labels"].as_array().ok_or("Expected labels")? {
            assert_eq!(
                serde_json::json!(chat_choice_labels(
                    case["options"].as_array().ok_or("Expected options")?
                )?),
                case["labels"]
            );
        }
        for case in cases["forms"].as_array().ok_or("Expected forms")? {
            let locale = case["locale"].as_str().ok_or("Expected locale")?;
            let actual = begin_chat_form(
                &case["form"],
                case["timezone"].as_str().ok_or("Expected timezone")?,
                locale,
            );
            let actual = match actual {
                Ok(start) => {
                    let mut state = start.state;
                    let count = presentation::steps(
                        &case["form"],
                        state["field_order"]
                            .as_array()
                            .ok_or("Expected field order")?,
                    )?
                    .len();
                    let mut prompts = Vec::new();
                    for step in 0..count {
                        state["step"] = serde_json::json!(step);
                        prompts.push(chat_form_prompt(&case["form"], &state)?);
                    }
                    state["step"] = serde_json::json!(0);
                    serde_json::json!({"state":state,"reply":start.reply,"prompts":prompts})
                }
                Err(ChatFormError::Answer(error)) => serde_json::json!({"answer_error":error}),
                Err(ChatFormError::Invalid) => serde_json::json!({"invalid":true}),
                Err(ChatFormError::DecimalRange) => {
                    serde_json::json!({"runtime_error":"InvalidOperation"})
                }
            };
            assert_eq!(actual, case["outcome"], "{case}");
        }
        let boundary_now =
            StoredInstant::from_legacy_input(&serde_json::json!("2026-11-19T10:00:00+00:00"))
                .ok_or("Expected calendar clock")?;
        for case in cases["calendar"]
            .as_array()
            .ok_or("Expected timezone catalog")?
        {
            let timezone = case["timezone"]
                .as_str()
                .ok_or("Expected catalog timezone")?;
            use base64::Engine;
            let hash = case["tzif_hash"]
                .as_str()
                .ok_or("Expected frozen timezone hash")?;
            let bytes = base64::engine::general_purpose::STANDARD.decode(
                cases["timezone_data"][hash]
                    .as_str()
                    .ok_or("Expected frozen timezone bytes")?,
            )?;
            assert_eq!(crate::sha256_hex(&bytes), hash);
            let zone = jiff::tz::TimeZone::tzif(timezone, &bytes)?;
            for (answer, expected) in cases["calendar_answers"]
                .as_array()
                .ok_or("Expected calendar answers")?
                .iter()
                .zip(
                    case["outcomes"]
                        .as_array()
                        .ok_or("Expected boundary outcomes")?,
                )
            {
                let actual = parse_time(
                    answer.as_str().ok_or("Expected boundary answer")?,
                    boundary_now.into(),
                    "en",
                    || Some(zone.clone()),
                )
                .map(|at| serde_json::json!(OffsetTimestamp(at).to_string()));
                assert_eq!(outcome(actual), *expected, "{timezone}: {answer}");
            }
        }
        for case in cases["times"].as_array().ok_or("Expected clocks")? {
            let now =
                StoredInstant::legacy_input_with_offset(&case["now"]).ok_or("Expected clock")?;
            let actual = parse_chat_form_time(
                case["answer"].as_str().ok_or("Expected answer")?,
                case["timezone"].as_str().ok_or("Expected zone")?,
                now,
                case["locale"].as_str().ok_or("Expected locale")?,
            )
            .map(|value| serde_json::json!(OffsetTimestamp(value).to_string()));
            assert_eq!(outcome(actual), case["outcome"], "{case}");
        }
        Ok(())
    }
}
