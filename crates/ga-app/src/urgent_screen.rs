//! Local diary safety screen for explicit current emergencies.

mod patterns;

use std::{error::Error, fmt, sync::OnceLock};

use fancy_regex::{Captures, Regex};

use patterns::*;

#[derive(Debug)]
pub struct UrgentScreenUnavailable;

impl fmt::Display for UrgentScreenUnavailable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Local diary safety screen is unavailable")
    }
}

impl Error for UrgentScreenUnavailable {}

impl From<fancy_regex::Error> for UrgentScreenUnavailable {
    fn from(_: fancy_regex::Error) -> Self {
        Self
    }
}

struct Rules {
    russian_history: Regex,
    russian_trailing_clause: Regex,
    russian_recurrence: Regex,
    third_party: Regex,
    russian_clause_separator: Regex,
    russian_history_prelude: Regex,
    russian_current_prelude: Regex,
    recurrence: Regex,
    pain_recurrence: Regex,
    again_in_history: Regex,
    distant: Vec<(Regex, bool)>,
    prior_week: Regex,
    immediate: Regex,
    face_and_arm: Regex,
    face: Regex,
    arm: Regex,
    negated_fast_sign: Regex,
    fast_current_prelude: Regex,
    date: Regex,
    date_suffix: Regex,
    historical_marker: Regex,
    general_question: Regex,
    self_report: Regex,
    urgent: Vec<Regex>,
    absent: Regex,
    double_negative: Regex,
    negative_prefix: Regex,
}

fn insensitive(pattern: &str) -> Result<Regex, fancy_regex::Error> {
    Regex::new(&format!("(?i){pattern}"))
}

fn anchored(pattern: &str) -> Result<Regex, fancy_regex::Error> {
    insensitive(&format!(r"\A(?:{pattern})"))
}

impl Rules {
    fn new() -> Result<Self, fancy_regex::Error> {
        Ok(Self {
            russian_history: anchored(RUSSIAN_HISTORY)?,
            russian_trailing_clause: anchored(RUSSIAN_TRAILING_CLAUSE)?,
            russian_recurrence: insensitive(RUSSIAN_RECURRENCE)?,
            third_party: insensitive(THIRD_PARTY)?,
            russian_clause_separator: insensitive(RUSSIAN_CLAUSE_SEPARATOR)?,
            russian_history_prelude: insensitive(RUSSIAN_HISTORY_PRELUDE)?,
            russian_current_prelude: insensitive(RUSSIAN_CURRENT_PRELUDE)?,
            recurrence: insensitive(&format!(
                r"\A(?:{AGAIN_NOW})|{RECURRENCE_NOW}|{REPEATED_EMERGENCY_NOW}|{RECURRENCE_AFTER}|{REPEATED_SYMPTOM_NOW}|{SYMPTOM_RETURNED}"
            ))?,
            pain_recurrence: insensitive(&format!(r"\A(?:{PAIN_ONGOING})|{PAIN_NOW}"))?,
            again_in_history: insensitive(AGAIN_IN_HISTORY)?,
            distant: [
                (DISTANT_HISTORY_1, false),
                (DISTANT_HISTORY_2, true),
                (DISTANT_HISTORY_3, true),
                (DISTANT_HISTORY_4, true),
                (DISTANT_HISTORY_5, false),
            ]
            .into_iter()
            .map(|(pattern, pain)| Ok((insensitive(pattern)?, pain)))
            .collect::<Result<_, fancy_regex::Error>>()?,
            prior_week: anchored(PRIOR_WEEK)?,
            immediate: insensitive(&format!("{BREATHING}|{BLEEDING}|{CHEST_AND_SWEAT}"))?,
            face_and_arm: insensitive(&format!(
                r"(?s)(?:{FACE}[^.!?;]{{0,80}}{ARM}|{ARM}[^.!?;]{{0,80}}{FACE})"
            ))?,
            face: insensitive(FACE)?,
            arm: insensitive(ARM)?,
            negated_fast_sign: insensitive(NEGATED_FAST_SIGN)?,
            fast_current_prelude: insensitive(FAST_CURRENT_PRELUDE)?,
            date: insensitive(DATE)?,
            date_suffix: anchored(DATE_SUFFIX)?,
            historical_marker: insensitive(HISTORICAL_MARKER)?,
            general_question: anchored(GENERAL_QUESTION)?,
            self_report: insensitive(SELF_REPORT)?,
            urgent: URGENT
                .iter()
                .map(|pattern| insensitive(&format!("(?s){pattern}")))
                .collect::<Result<_, _>>()?,
            absent: anchored(ABSENT)?,
            double_negative: insensitive(DOUBLE_NEGATIVE)?,
            negative_prefix: insensitive(NEGATIVE_PREFIX)?,
        })
    }

    fn russian_history(&self, text: &str, year: i32) -> Result<bool, UrgentScreenUnavailable> {
        let Some(history) = self.russian_history.captures(text)? else {
            return Ok(false);
        };
        let end = history.get(0).ok_or(UrgentScreenUnavailable)?.end();
        let tail = &text[end..];
        if text.trim_start().starts_with([',', ';'])
            && self.russian_trailing_clause.is_match(tail)?
        {
            return Ok(false);
        }
        let distant = history.name("old").is_some()
            || history
                .name("year")
                .is_some_and(|matched| earlier_year(matched.as_str(), year))
            || history
                .name("count")
                .is_some_and(|matched| at_least_two(matched.as_str()));
        Ok(distant && !self.russian_recurrence.is_match(tail)?)
    }

    fn third_party(&self, text: &str, year: i32) -> Result<bool, UrgentScreenUnavailable> {
        for emergency in self.third_party.find_iter(text) {
            let emergency = emergency?;
            if emergency.as_str().to_lowercase().starts_with('у')
                && emergency
                    .as_str()
                    .chars()
                    .nth(1)
                    .is_some_and(char::is_whitespace)
            {
                let prefix = self
                    .russian_clause_separator
                    .split(&text[..emergency.start()])
                    .last()
                    .transpose()?
                    .unwrap_or("");
                if let Some(history) = self.russian_history_prelude.find(prefix)?
                    && !self
                        .russian_current_prelude
                        .is_match(&prefix[history.end()..])?
                {
                    continue;
                }
                if self.russian_history(&text[emergency.end()..], year)? {
                    continue;
                }
            }
            return Ok(true);
        }
        Ok(false)
    }

    fn current_recurrence(&self, text: &str, pain: bool) -> Result<bool, UrgentScreenUnavailable> {
        Ok(self.recurrence.is_match(text)? || (pain && self.pain_recurrence.is_match(text)?))
    }

    fn without_distant_history(
        &self,
        mut text: String,
        year: i32,
    ) -> Result<String, UrgentScreenUnavailable> {
        for (history, pain) in &self.distant {
            let mut output = String::with_capacity(text.len());
            let mut previous = 0;
            for found in history.captures_iter(text.as_str()) {
                let found = found?;
                let phrase = found.get(0).ok_or(UrgentScreenUnavailable)?;
                output.push_str(&text[previous..phrase.start()]);
                if !english_history(&found, year)
                    || self.current_recurrence(&text[phrase.end()..], *pain)?
                    || (*pain && self.again_in_history.is_match(phrase.as_str())?)
                {
                    output.push_str(phrase.as_str());
                }
                previous = phrase.end();
            }
            output.push_str(&text[previous..]);
            text = output;
        }
        Ok(text)
    }

    fn face_and_arm(&self, text: &str, year: i32) -> Result<bool, UrgentScreenUnavailable> {
        for combined in self.face_and_arm.find_iter(text) {
            let combined = combined?;
            let matched = combined.as_str();
            let mut negated = false;
            for sign in [&self.face, &self.arm] {
                if let Some(sign) = sign.find(matched)?
                    && self.negated_fast_sign.is_match(preceding(
                        text,
                        combined.start() + sign.start(),
                        35,
                    ))?
                {
                    negated = true;
                }
            }
            if negated {
                continue;
            }
            let suffix = &text[combined.end()..];
            let prelude = preceding(text, combined.start(), 60)
                .rsplit(['.', '!', '?', ';'])
                .next()
                .unwrap_or("");
            let historical_date = if let Some(date) = self.date.captures(prelude)? {
                Some(date)
            } else if let Some(date) = self.date.captures(matched)? {
                Some(date)
            } else {
                self.date_suffix.captures(suffix)?
            };
            let marker = format!("{prelude}{matched}{}", leading(suffix, 50));
            let historical = historical_date.is_some_and(|date| english_history(&date, year))
                || self.historical_marker.is_match(&marker)?;
            if !historical
                || self.fast_current_prelude.is_match(prelude)?
                || self.current_recurrence(suffix, false)?
            {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn urgent(&self, text: &str, year: i32) -> Result<bool, UrgentScreenUnavailable> {
        let cleaned = self.without_distant_history(normalize_quotes(text), year)?;
        let mut text = cleaned.as_str();
        if let Some(old) = self.prior_week.find(text)?
            && !self.current_recurrence(&text[old.end()..], true)?
        {
            text = &text[old.end()..];
        }
        if self.immediate.is_match(text)?
            || self.face_and_arm(text, year)?
            || self.third_party(text, year)?
        {
            return Ok(true);
        }
        if self.general_question.is_match(text)? && !self.self_report.is_match(text)? {
            return Ok(false);
        }
        for pattern in &self.urgent {
            for urgent in pattern.find_iter(text) {
                let urgent = urgent?;
                let suffix = &text[urgent.end()..];
                if self.russian_history(suffix, year)? || self.absent.is_match(suffix)? {
                    continue;
                }
                let prefix = preceding(text, urgent.start(), 40);
                if self.double_negative.is_match(prefix)?
                    || !self.negative_prefix.is_match(prefix)?
                {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }
}

fn preceding(text: &str, end: usize, count: usize) -> &str {
    let start = text[..end]
        .char_indices()
        .rev()
        .nth(count - 1)
        .map_or(0, |(start, _)| start);
    &text[start..end]
}

fn leading(text: &str, count: usize) -> &str {
    &text[..text
        .char_indices()
        .nth(count)
        .map_or(text.len(), |(end, _)| end)]
}

fn normalize_quotes(text: &str) -> String {
    text.replace(['’', '‘'], "'")
}

// Unicode 17 decimal digit blocks, as used by the pinned Python classifier.
const DECIMAL_ZEROES: &[u32] = &[
    0x0030, 0x0660, 0x06f0, 0x07c0, 0x0966, 0x09e6, 0x0a66, 0x0ae6, 0x0b66, 0x0be6, 0x0c66, 0x0ce6,
    0x0d66, 0x0de6, 0x0e50, 0x0ed0, 0x0f20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80,
    0x1a90, 0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
    0xff10, 0x104a0, 0x10d30, 0x10d40, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450,
    0x114d0, 0x11650, 0x116c0, 0x116d0, 0x116da, 0x11730, 0x118e0, 0x11950, 0x11bf0, 0x11c50,
    0x11d50, 0x11da0, 0x11de0, 0x11f50, 0x16130, 0x16a60, 0x16ac0, 0x16b50, 0x16d70, 0x1ccf0,
    0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e5f1, 0x1e950,
    0x1fbf0,
];

fn decimal_number(value: &str) -> Option<u32> {
    value.chars().try_fold(0u32, |number, character| {
        let codepoint = u32::from(character);
        let index = DECIMAL_ZEROES.partition_point(|zero| *zero <= codepoint);
        let digit = codepoint - DECIMAL_ZEROES[index.checked_sub(1)?];
        (digit < 10).then(|| number.saturating_mul(10).saturating_add(digit))
    })
}

fn earlier_year(value: &str, year: i32) -> bool {
    decimal_number(value)
        .and_then(|value| i32::try_from(value).ok())
        .is_some_and(|value| value < year - 1)
}

fn at_least_two(value: &str) -> bool {
    decimal_number(value).map_or_else(
        || value.chars().all(char::is_alphabetic),
        |value| value >= 2,
    )
}

fn english_history(captures: &Captures<'_, str>, year: i32) -> bool {
    captures
        .get(1)
        .is_some_and(|value| earlier_year(value.as_str(), year))
        || captures
            .get(2)
            .is_some_and(|value| at_least_two(value.as_str()))
}

fn rules() -> Result<&'static Rules, UrgentScreenUnavailable> {
    static RULES: OnceLock<Result<Rules, fancy_regex::Error>> = OnceLock::new();
    RULES
        .get_or_init(Rules::new)
        .as_ref()
        .map_err(|_| UrgentScreenUnavailable)
}

pub fn obvious_urgent_symptoms(text: &str) -> Result<bool, UrgentScreenUnavailable> {
    rules()?.urgent(text, time::OffsetDateTime::now_utc().year())
}

pub fn obvious_third_party_emergency(text: &str) -> Result<bool, UrgentScreenUnavailable> {
    rules()?.third_party(
        &normalize_quotes(text),
        time::OffsetDateTime::now_utc().year(),
    )
}

pub fn urgent_notice(locale: &str) -> &'static str {
    if locale
        .split(['-', '_'])
        .next()
        .is_some_and(|code| code.eq_ignore_ascii_case("ru"))
    {
        "При внезапных тяжёлых симптомах нужна срочная медицинская помощь: позвоните 112 или в местную экстренную службу. Не ждите оценки по данным часов."
    } else {
        "Sudden severe symptoms need urgent medical help. Call 112 or your local emergency service. Do not wait for an assessment from watch data."
    }
}
