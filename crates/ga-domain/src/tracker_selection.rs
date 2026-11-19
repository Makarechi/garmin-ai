//! Local matching of tracker labels, using the frozen Python text rules.

use std::{collections::BTreeSet, sync::OnceLock};

use fancy_regex::{Regex, RegexBuilder};

use crate::{event_payload::python_whitespace, event_schema::chat_form::casefold};

mod unicode;

const FILLER: &[&str] = &[
    "a", "an", "as", "at", "be", "by", "do", "i", "if", "in", "is", "it", "my", "of", "on", "or",
    "so", "to", "up", "we", "я", "в", "и", "на", "не", "по", "за", "от", "из", "до",
];

#[derive(Debug)]
pub struct SelectionError;

impl std::fmt::Display for SelectionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Tracker selection text could not be matched")
    }
}
impl std::error::Error for SelectionError {}

impl From<fancy_regex::Error> for SelectionError {
    fn from(_: fancy_regex::Error) -> Self {
        Self
    }
}

struct Patterns {
    cue: Regex,
    builtin: Regex,
    qualifiers: Regex,
    marker: Regex,
    leading: Regex,
    explicit: Regex,
    proposal: Regex,
}

fn patterns() -> Result<&'static Patterns, SelectionError> {
    static PATTERNS: OnceLock<Result<Patterns, SelectionError>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        Ok(Patterns {
        cue: compile(r"^(?:(?:я|I)\s+)?(?:записал[аи]?|запиши(?:те)?|записать|добавить|отметить|отметил[аи]?|внёс|внес|внесла|внести|add|added|log|logged|record|recorded|track|tracked)\b")?,
        builtin: compile(r"\b(?:coffees?|caffeine|кофе|кофеин|medications?|medicines?|лекарств\w*|таблетк\w*|alcohol|алкогол\w*|migraines?|мигрен\w*|headaches?|головн\w*\s+бол\w*|hydration|water|вод(?:а|ы|е|у|ой|ою)|meals?|foods?|breakfasts?|lunch(?:es)?|dinners?|ед(?:а|ы|е|у|ой|ою)|завтрак\w*|обед\w*|ужин\w*|illness(?:es)?|болезн\w*|naps?|sleep|сон|дрем\w*|stressors?|stress|стресс\w*|travel|поездк\w*|moods?|настроен\w*|activit(?:y|ies)|exercises?|workouts?|тренировк\w*|symptoms?|симптом\w*|pain|бол(?:ь|и|ей|ями|ит|ят|ела|ело|ели|еет|еют)|energy|энерги\w*|note|notes|заметк\w*)\b")?,
        qualifiers: compile(r"^(?:(?:my|the|a|an|today's|yesterday's|current|morning|afternoon|evening|nightly|daily|weekly|monthly|мой|моя|моё|мои|мою|свою|сегодняшн\w*|вчерашн\w*|утренн\w*|дневн\w*|вечерн\w*|ежедневн\w*)\s+){1,3}")?,
        marker: compile(r"\b(?:tracker|трекер)\b")?,
        leading: compile(r"^(?:(?:my|the|a|an|мой|моя|моё|мои)\s+)*(?:(?:tracker|трекер)\s+)?")?,
        explicit: compile(r"^(?:(?:my|the|a|an|мой|моя|моё|мои)\s+)*(?:tracker|трекер)\s+\S")?,
        proposal: compile(r"\b(?:хочу|давай|нужно|можно)\s+(?:начать\s+)?(?:отслеживать|записывать|вести)\b|\b(?:i\s+want\s+to|let(?:'s| us)|start)\s+(?:track|log|record)\b")?,
        })
    }).as_ref().map_err(|_| SelectionError)
}

fn compile(source: &str) -> Result<Regex, SelectionError> {
    let alnum = unicode::ALNUM_CLASS;
    let word = format!("[{alnum}_]");
    let boundary = format!("(?:(?<!{word})(?={word})|(?<={word})(?!{word}))");
    let mut pattern = String::new();
    let mut chars = source.chars();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            match chars.next().ok_or(SelectionError)? {
                'w' => pattern.push_str(&word),
                'W' => pattern.push_str(&format!("[^{alnum}_]")),
                'b' => pattern.push_str(&boundary),
                's' => pattern.push_str(r"[\s\x{1c}-\x{1f}]"),
                'S' => pattern.push_str(r"[^\s\x{1c}-\x{1f}]"),
                escaped => {
                    pattern.push('\\');
                    pattern.push(escaped);
                }
            }
        } else if matches!(ch, 'i' | 'I') {
            pattern.push_str("[iIİı]");
        } else {
            pattern.push(ch);
        }
    }
    Ok(RegexBuilder::new(&pattern).case_insensitive(true).build()?)
}

fn alnum(ch: char) -> bool {
    let point = u32::from(ch);
    let index = unicode::ALNUM_RANGES.partition_point(|&(_, end)| end < point);
    unicode::ALNUM_RANGES
        .get(index)
        .is_some_and(|&(start, _)| start <= point)
}

fn word(ch: char) -> bool {
    ch == '_' || alnum(ch)
}
fn punctuation(ch: char) -> bool {
    " \t:,.!?".contains(ch)
}
fn tokens(text: &str) -> Vec<&str> {
    text.split(|ch| !alnum(ch))
        .filter(|token| !token.is_empty())
        .collect()
}
fn terms(text: &str) -> BTreeSet<String> {
    tokens(text)
        .into_iter()
        .filter(|token| token.chars().count() >= 3 || !FILLER.contains(token))
        .map(str::to_owned)
        .collect()
}

pub fn tracker_selection_cue(text: &str) -> Result<bool, SelectionError> {
    let patterns = patterns()?;
    let text = text.trim_matches(python_whitespace);
    let Some(cue) = patterns.cue.find(text)? else {
        return Ok(false);
    };
    let target = text[cue.end()..].trim_matches(punctuation);
    let normalized = target.replace('’', "'");
    let reserved = match patterns.qualifiers.find(&normalized)? {
        Some(found) => &normalized[found.end()..],
        None => &normalized,
    };
    let builtin = patterns
        .builtin
        .find(reserved)?
        .is_some_and(|found| found.start() == 0);
    Ok(!target.is_empty() && (!builtin || patterns.marker.is_match(target)?))
}

pub fn is_tracker_proposal(text: &str) -> Result<bool, SelectionError> {
    Ok(patterns()?.proposal.is_match(text)?)
}

pub struct TrackerSelection {
    target: String,
    literal: String,
    short: String,
    wanted: BTreeSet<String>,
    reserved: bool,
    explicit: bool,
}

pub struct TrackerLabel {
    label: String,
    folded: String,
    names: BTreeSet<String>,
}

impl TrackerLabel {
    pub fn new(label: &str) -> Self {
        let folded = casefold(label);
        Self {
            label: label.to_owned(),
            names: terms(&folded),
            folded,
        }
    }
}

fn caseless_equal(left: char, right: char) -> bool {
    let lower = |ch| {
        let point = u32::from(ch);
        unicode::RE_LOWER
            .binary_search_by_key(&point, |&(code, _)| code)
            .map_or(point, |index| unicode::RE_LOWER[index].1)
    };
    let left = lower(left);
    let right = lower(right);
    left == right || unicode::RE_EXTRA.binary_search(&(left, right)).is_ok()
}

fn label_prefix(label: &str, text: &str) -> Option<usize> {
    let mut text = text.char_indices();
    let mut end = 0;
    for expected in label.chars() {
        let (index, actual) = text.next()?;
        if !caseless_equal(expected, actual) {
            return None;
        }
        end = index + actual.len_utf8();
    }
    Some(end)
}

impl TrackerSelection {
    pub fn new(text: &str) -> Result<Option<Self>, SelectionError> {
        let patterns = patterns()?;
        let text = text.trim_matches(python_whitespace);
        let Some(cue) = patterns.cue.find(text)? else {
            return Ok(None);
        };
        let literal = text[cue.end()..].trim_matches(python_whitespace);
        let target = literal.trim_matches(punctuation);
        let reserved = !tracker_selection_cue(text)?;
        if target.is_empty() || (reserved && literal == target) {
            return Ok(None);
        }
        let short = &target[patterns.leading.find(target)?.ok_or(SelectionError)?.end()..];
        let explicit = patterns.explicit.is_match(target)?;
        let literal = if explicit {
            &literal[patterns.leading.find(literal)?.ok_or(SelectionError)?.end()..]
        } else {
            literal
        };
        let target = casefold(if explicit {
            short.trim_matches(punctuation)
        } else {
            target
        });
        Ok(Some(Self {
            wanted: terms(&target),
            target,
            literal: casefold(literal),
            short: short.to_owned(),
            reserved,
            explicit,
        }))
    }

    pub fn score(&self, label: &TrackerLabel) -> Result<Option<usize>, SelectionError> {
        if self.reserved && self.literal != label.folded {
            return Ok(None);
        }
        let prefix_end = label_prefix(&label.label, &self.short);
        let ends_at_boundary = prefix_end.is_some_and(|end| {
            self.short[..end].chars().next_back().is_some_and(word)
                != self.short[end..].chars().next().is_some_and(word)
        });
        if let Some(end) = prefix_end
            && !self.short[end..].chars().next().is_some_and(word)
            && patterns()?.builtin.is_match(&self.short[end..])?
        {
            return Ok(None);
        }
        let mut overlap = self.wanted.intersection(&label.names).count();
        if !self.target.is_empty() && self.target == label.folded {
            overlap = self.wanted.len() + 1;
        }
        if self.literal == label.folded {
            overlap = self.wanted.len() + 2;
        }
        let short_word = self.short.split(|ch| !alnum(ch)).next().unwrap_or("");
        if label.label.chars().count() <= 2
            && !label.label.is_empty()
            && label.label.chars().all(alnum)
            && casefold(short_word) == label.folded
            && (!FILLER.contains(&label.folded.as_str()) || self.explicit)
        {
            overlap = overlap.max(1);
        }
        let label_tokens = tokens(&label.folded);
        if label_tokens.len() > 1 && label_tokens.iter().all(|token| token.chars().count() <= 2) {
            let words: Vec<_> = self
                .target
                .split(|ch| !word(ch))
                .filter(|s| !s.is_empty())
                .collect();
            if words
                .windows(label_tokens.len())
                .any(|window| window == label_tokens)
            {
                overlap = overlap.max(label_tokens.len());
            }
        }
        if (label_tokens.len() == 1 && overlap == 1 && self.wanted.len() > 1 && !ends_at_boundary)
            || (label.names.len() > 1 && overlap == 1 && self.wanted.len() > 1)
        {
            return Ok(None);
        }
        Ok((overlap > 0).then_some(overlap))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    #[test]
    fn compact_class_matches_every_frozen_unicode_scalar() -> Result<(), Box<dyn std::error::Error>>
    {
        let class = Regex::new(&format!("^{}$", unicode::ALNUM_CLASS))?;
        for point in 0..=0x10ffff {
            if let Some(ch) = char::from_u32(point) {
                assert_eq!(
                    class.is_match(ch.encode_utf8(&mut [0; 4]))?,
                    alnum(ch),
                    "U+{point:04X}"
                );
            }
        }
        Ok(())
    }

    #[test]
    fn frozen_tracker_selection() -> Result<(), Box<dyn std::error::Error>> {
        let groups: Value =
            serde_json::from_str(include_str!("../contracts/tracker-chat-selection.json"))?;
        for group in groups.as_array().ok_or("Expected frozen groups")? {
            let labels: Vec<_> = group["labels"]
                .as_array()
                .ok_or("Expected frozen labels")?
                .iter()
                .map(|label| {
                    Ok(TrackerLabel::new(
                        label.as_str().ok_or("Expected label text")?,
                    ))
                })
                .collect::<Result<_, Box<dyn std::error::Error>>>()?;
            for case in group["cases"].as_array().ok_or("Expected frozen cases")? {
                let text = case[0].as_str().ok_or("Expected request text")?;
                assert_eq!(tracker_selection_cue(text)?, case[1], "cue {text:?}");
                assert_eq!(is_tracker_proposal(text)?, case[2], "proposal {text:?}");
                let mut matches = Vec::new();
                if let Some(selection) = TrackerSelection::new(text)? {
                    for (index, label) in labels.iter().enumerate() {
                        if let Some(score) = selection.score(label)? {
                            matches.push((score, index));
                        }
                    }
                }
                matches.sort_by_key(|&(score, index)| (std::cmp::Reverse(score), index));
                let best = matches.first().map(|&(score, _)| score);
                let selected: Vec<_> = matches
                    .into_iter()
                    .filter(|&(score, _)| Some(score) == best)
                    .take(5)
                    .map(|(_, index)| index)
                    .collect();
                assert_eq!(
                    serde_json::json!(selected),
                    case[3],
                    "selection {text:?}, labels={}",
                    group["labels"]
                );
            }
        }
        Ok(())
    }
}
