//! Literal dose text from the existing Russian and English diary input contract.

use std::{borrow::Cow, error::Error, fmt, sync::OnceLock};

use fancy_regex::{Captures, Regex, Replacer};
use ga_domain::metric_contract::decimal_value;

const MAX_TEXT_BYTES: usize = 64_000;
const VERB: &str = r"\b(?:принял[аи]?|выпил[аи]?|пил[аи]?|принимал[аи]?|проглотил[аи]?|took|taken|swallowed|съел[аи]?(?=\s+(?:(?:одну|две|\d+)\s+)?(?:таблетк\w*|капсул\w*|лекарств\w*)\b))\b";
const QUANTITY: &str = r"(?:\d+(?:[.,]\d+)?|один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|one|two|three|four|five|six|seven|eight|nine|ten|an|a)";
const DOSE: &str = r"\b\d+(?:[.,]\d+)?\s*(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблет(?:к[ауие]?|ок)|tablets?|pills?|капсул[ауые]?|capsules?|кап(?:ля|ли|ель)|drops?)\b";
const NUMBER_WORDS: &[(&str, u8)] = &[
    ("один", 1),
    ("одну", 1),
    ("два", 2),
    ("две", 2),
    ("три", 3),
    ("четыре", 4),
    ("пять", 5),
    ("шесть", 6),
    ("семь", 7),
    ("восемь", 8),
    ("девять", 9),
    ("десять", 10),
    ("one", 1),
    ("an", 1),
    ("a", 1),
    ("two", 2),
    ("three", 3),
    ("four", 4),
    ("five", 5),
    ("six", 6),
    ("seven", 7),
    ("eight", 8),
    ("nine", 9),
    ("ten", 10),
];

#[derive(Debug, PartialEq)]
pub struct MedicationDose {
    pub amount: f64,
    pub unit: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DoseTextError {
    InputTooLarge,
    Invalid,
}

impl fmt::Display for DoseTextError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InputTooLarge => "Medication text exceeds its input limit",
            Self::Invalid => "Medication dose needs explicit text evidence",
        })
    }
}
impl Error for DoseTextError {}
impl From<fancy_regex::Error> for DoseTextError {
    fn from(_: fancy_regex::Error) -> Self {
        Self::Invalid
    }
}

struct Patterns {
    micro: Regex,
    space_groups: Regex,
    units: Vec<(Regex, &'static str)>,
    english: Regex,
    cyrillic: Regex,
    comma_groups: Regex,
    fraction: Regex,
    capsules: Regex,
    mixed_half: Regex,
    plain_half: Regex,
    named_count: Regex,
    unknown_name: Regex,
    number_words: Regex,
    literal: Regex,
    alternatives: Regex,
}

impl Patterns {
    fn new() -> Result<Self, fancy_regex::Error> {
        let units = [
            (r"milligrams?|миллиграмм(?:а|ов)?", "mg"),
            (r"micrograms?|микрограмм(?:а|ов)?", "mcg"),
            (r"millilit(?:er|re)s?|миллилитр(?:а|ов)?", "ml"),
            (r"grams?|грамм(?:а|ов)?", "g"),
            (
                r"international\s+units?|международн(?:ых|ые)\s+единиц(?:ы)?",
                "iu",
            ),
        ]
        .into_iter()
        .map(|(words, unit)| {
            Regex::new(&format!(r"(?i)(\b{QUANTITY}\s*)({words})\b")).map(|pattern| (pattern, unit))
        })
        .collect::<Result<_, _>>()?;
        let words = NUMBER_WORDS
            .iter()
            .map(|(word, _)| *word)
            .collect::<Vec<_>>()
            .join("|");
        Ok(Self {
            micro: Regex::new(r"(?i)(?<=\d)\s*[µμ]g\b")?,
            space_groups: Regex::new(
                r"(?i)(?<![\d.,])\b\d{1,3}(?:[ \x{a0}\x{202f}]\d{3})+(?:[.,]\d+)?(?=\s*(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|milligrams?|миллиграмм(?:а|ов)?)\b)",
            )?,
            units,
            english: Regex::new(r"(?i)\b(?:I|took|taken|dose|change|correct)\b")?,
            cyrillic: Regex::new(r"(?i)[а-яё]")?,
            comma_groups: Regex::new(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b")?,
            fraction: Regex::new(
                r"\b(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)(?=\s+(?:таблет|tablet|мг|mg|мл|ml|кап|drop))",
            )?,
            capsules: Regex::new(
                r"(?i)\b(one|two|three|four|five|six|seven|eight|nine|ten)(?=\s+capsules?\b)",
            )?,
            mixed_half: Regex::new(&format!(
                r"(?i)\b({QUANTITY})\s+and\s+a\s+half\s+(?=tablets?\b|pills?\b)"
            ))?,
            plain_half: Regex::new(
                r"(?i)\b(?:half\s+(?:a\s+)?(?=tablets?\b|pills?\b)|половин[ау]\s+(?=таблетки\b))",
            )?,
            named_count: Regex::new(&format!(
                r"(?i)({VERB})\s+({QUANTITY}|a|an)\s+([\w/-]+(?:\s+[\w/-]+){{0,3}}?)\s+(tablets?|pills?|таблетк[ауи]|таблеток)\b"
            ))?,
            unknown_name: Regex::new(
                r"(?i)\b(?:неизвестн\w*|какую-то|какой-то|какое-то|какие-то|unknown|some|something|что-то)\b",
            )?,
            number_words: Regex::new(&format!(
                r"(?i)\b({words})(?=\s+(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблет|tablets?|pills?|кап|drops?))"
            ))?,
            literal: Regex::new(r"\A(\d+(?:[.,]\d+)?)\s*(.+)\z")?,
            alternatives: Regex::new(&format!(
                r"(?i)\b\d+(?:[.,]\d+)?\s*(?:[-–—]|to|до|or|или|либо)\s*(?:{DOSE})"
            ))?,
        })
    }
}

fn patterns(text: &str) -> Result<&'static Patterns, DoseTextError> {
    static RULES: OnceLock<Option<Patterns>> = OnceLock::new();
    if text.len() > MAX_TEXT_BYTES {
        return Err(DoseTextError::InputTooLarge);
    }
    RULES
        .get_or_init(|| Patterns::new().ok())
        .as_ref()
        .ok_or(DoseTextError::Invalid)
}

/// Reuse the diary's existing lexical forms without filling in absent details.
pub fn normalize_dose_text(source: &str) -> Result<String, DoseTextError> {
    let rules = patterns(source)?;
    let mut text = source.to_owned();
    replace(&mut text, &rules.micro, " mcg")?;
    replace(
        &mut text,
        &rules.space_groups,
        |matched: &Captures<'_, str>| {
            matched[0]
                .chars()
                .filter(|character| !whitespace(*character))
                .collect::<String>()
        },
    )?;
    for (pattern, unit) in &rules.units {
        replace(&mut text, pattern, |matched: &Captures<'_, str>| {
            format!("{}{unit}", &matched[1])
        })?;
    }
    if rules.english.is_match(&text)? && !rules.cyrillic.is_match(&text)? {
        replace(
            &mut text,
            &rules.comma_groups,
            |matched: &Captures<'_, str>| matched[0].replace(',', ""),
        )?;
    }
    replace(&mut text, &rules.fraction, |matched: &Captures<'_, str>| {
        format_fraction(matched)
    })?;
    replace(&mut text, &rules.capsules, |matched: &Captures<'_, str>| {
        normalized_count(&matched[1])
    })?;
    replace(
        &mut text,
        &rules.mixed_half,
        |matched: &Captures<'_, str>| {
            number_word(&matched[1])
                .map(f64::from)
                .or_else(|| decimal(&matched[1]))
                .map_or_else(
                    || "? ".into(),
                    |whole| format!("{} ", python_float(whole + 0.5)),
                )
        },
    )?;
    replace(&mut text, &rules.plain_half, "0.5 ")?;
    replace(
        &mut text,
        &rules.named_count,
        |matched: &Captures<'_, str>| {
            if rules.unknown_name.is_match(&matched[3]).unwrap_or(true) {
                matched[0].to_owned()
            } else {
                format!(
                    "{} {} {} {}",
                    &matched[1],
                    &matched[3],
                    normalized_count(&matched[2]),
                    &matched[4]
                )
            }
        },
    )?;
    replace(
        &mut text,
        &rules.number_words,
        |matched: &Captures<'_, str>| normalized_count(&matched[1]),
    )?;
    Ok(text)
}

pub fn ambiguous_dose_text(source: &str) -> Result<bool, DoseTextError> {
    Ok(patterns(source)?.alternatives.is_match(source)?)
}

pub fn parse_literal_dose(source: &str) -> Result<MedicationDose, DoseTextError> {
    let matched = patterns(source)?
        .literal
        .captures(source)?
        .ok_or(DoseTextError::Invalid)?;
    let amount = decimal(&matched[1]).ok_or(DoseTextError::Invalid)?;
    let unit = matched[2].to_lowercase();
    let unit = match unit.as_str() {
        "мг" => "mg",
        "мкг" => "mcg",
        "мл" => "ml",
        "г" => "g",
        "ме" | "iu" => "IU",
        unit if unit.starts_with("таблет")
            || unit.starts_with("tablet")
            || unit.starts_with("pill") =>
        {
            "tablet"
        }
        unit if unit.starts_with("капсул") || unit.starts_with("capsule") => "capsule",
        unit if unit.starts_with("кап") || unit.starts_with("drop") => "drop",
        _ => unit.as_str(),
    };
    Ok(MedicationDose {
        amount,
        unit: unit.to_owned(),
    })
}

fn replace(
    text: &mut String,
    pattern: &Regex,
    replacement: impl Replacer,
) -> Result<(), DoseTextError> {
    if let Cow::Owned(updated) = pattern.try_replacen(text, 0, replacement)? {
        *text = updated;
    }
    Ok(())
}

fn whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

fn number_word(word: &str) -> Option<u8> {
    let normalized = word.to_lowercase().replace('ſ', "s");
    NUMBER_WORDS
        .iter()
        .find(|(word, _)| *word == normalized)
        .map(|(_, number)| *number)
}

fn normalized_count(word: &str) -> String {
    number_word(word).map_or_else(|| word.to_owned(), |number| number.to_string())
}

fn decimal(value: &str) -> Option<f64> {
    let ascii = value
        .chars()
        .map(|character| {
            if matches!(character, '.' | ',') {
                Some('.')
            } else {
                decimal_value(character).map(|digit| char::from(b'0' + digit))
            }
        })
        .collect::<Option<String>>()?;
    ascii.parse::<f64>().ok().filter(|value| value.is_finite())
}

fn format_fraction(matched: &Captures<'_, str>) -> String {
    if [1, 2, 3]
        .into_iter()
        .filter_map(|index| matched.get(index))
        .map(|part| part.as_str().chars().count())
        .sum::<usize>()
        >= 12
    {
        return "?".to_owned();
    }
    let whole = matched
        .get(1)
        .map_or(Some(0.0), |part| decimal(part.as_str()));
    match (whole, decimal(&matched[2]), decimal(&matched[3])) {
        (Some(whole), Some(numerator), Some(denominator)) if denominator > 0.0 => {
            python_float(whole + numerator / denominator)
        }
        _ => "?".to_owned(),
    }
}

fn python_float(value: f64) -> String {
    let text = format!("{value:?}");
    match text.split_once('e') {
        Some((mantissa, exponent)) => exponent
            .parse::<i16>()
            .map_or_else(|_| text.clone(), |exp| format!("{mantissa}e{exp:+03}")),
        None => text,
    }
}
