//! Literal ownership and medication names in Russian and English diary clauses.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::OnceLock,
};

use fancy_regex::{Captures, Regex};

use crate::medication_dose::{
    DOSE, DoseTextError, MAX_TEXT_BYTES, NUMBER_WORDS, QUANTITY, VERB, replace,
};

const CLOCK: &str = r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})|\bсейчас\b|\bnow\b|\b\d{1,2}:\d{2}\b|\bв\s+\d{1,2}(?::\d{2})?\b";
const UNIT: &str = r"(?:час(?:а|ов)?|минут(?:у|ы)?|hours?|minutes?)";
const OTHER: &str = r"\b(?:он|она|они|муж|жена|мама|папа|сын|дочь|реб[её]нок|брат|сестра|he|she|they|husband|wife|mother|father|son|daughter|врач|доктор|пациент|пациентка|сосед|соседка|коллега|друг|подруга|медсестра|медбрат|фельдшер|санитар|санитарка|doctor|nurse|patient|friend)\b";
const GENERIC: &str = r"\b(?:таблетк[ауи]|капсул[ауые]?|capsules?|лекарств[оа]|medications?|meds?|medicines?|tablets?|pills?|я|i|мы|we|сегодня|вчера|утром|вечером|утра|вечера|дня|ночи|свою|свой|свои|сво[её]|мою|мой|мои|мо[её]|my|our|the|уже|снова|ещ[её]|повторно|again|another|today|yesterday|just|already|have)\b";
const NEGATIVE: &str = r"\b(?:не|ничего|нет|no(?![-–—])|not|never|neither|nor|zero|ноль|ни|ли|(?:did|have|has|had|was|were|is|are|do|does)n['’]t)\b|^\s*(?:did|have|has|had|was|were|is|are|do|does|when|why|what|how)\b";
const UNKNOWN: &str =
    r"\b(?:неизвестн\w*|какую-то|какой-то|какое-то|какие-то|unknown|some|something|что-то)\b";
const UNKNOWN_DETAIL: &str = r"\b(?:и\s+)?не\s+(?:помню|знаю)\b|\b(?:I\s+)?(?:do not|don['’]t)\s+(?:remember|know)\s+(?:the\s+)?(?:dose|name|unit)\b";
const REASON: &str = r"\b(?:because|потому\s+что|так\s+как)\b";
const APPROXIMATE: &str = r"\b(?:примерно|около|приблизительно|around|about|approximately)\b";
const QUOTED_NAME: &str = r#"«[^»]*»|"[^"]*""#;
const COMMA: &str = r"(?<!\d),|,(?!\d)";
const OWNER: &str = r"\b(?:я|мы|I|we)\b";
const DATE: &str = r"\b\d{4}-\d{2}-\d{2}\b";

fn insensitive(pattern: &str) -> Result<Regex, fancy_regex::Error> {
    Regex::new(&format!("(?i){pattern}"))
}

struct Rules {
    verb: Regex,
    owner: Regex,
    negative: Regex,
    other: Regex,
    generic_name: Regex,
    qualifiers: Regex,
    with: Regex,
    medicine_before_verb: Regex,
    russian_singular_owner: Regex,
    trailing_person: Regex,
    implicit_subject: Regex,
    adjective_ending: Regex,
    pronoun_object: Regex,
    reference: Regex,
    non_medical: Regex,
    zero_count: Regex,
    drink: Regex,
    medicine_context: Regex,
    owner_prefix: Vec<Regex>,
    quotes: Regex,
    clock: Regex,
    approximate_context: Regex,
    phrase_noise: Vec<Regex>,
    phrase_ending: Regex,
    purpose: Regex,
    alternative: Regex,
    named_count: Regex,
    name_noise: Vec<Regex>,
    bare_dose_marker: Regex,
    numeric_name: Regex,
    name_prefix: Regex,
    conjunction: Regex,
    name: Regex,
    numeric_designation: Regex,
    russian_consonant: Regex,
    russian_vowel: Regex,
}

impl Rules {
    fn new() -> Result<Self, fancy_regex::Error> {
        let relative =
            format!(r"\b(?:(?:{QUANTITY}\s+)?{UNIT}|{UNIT}\s+{QUANTITY})\s+(?:назад|ago)\b");
        let words = NUMBER_WORDS
            .iter()
            .map(|(word, _)| *word)
            .collect::<Vec<_>>()
            .join("|");
        let counts = NUMBER_WORDS
            .iter()
            .filter_map(|(word, _)| (!matches!(*word, "a" | "an")).then_some(*word))
            .collect::<Vec<_>>()
            .join("|");
        Ok(Self {
            verb: insensitive(VERB)?,
            owner: insensitive(OWNER)?,
            negative: insensitive(NEGATIVE)?,
            other: insensitive(OTHER)?,
            generic_name: insensitive(&format!(r"\A{GENERIC}\z"))?,
            qualifiers: insensitive(&format!("{UNKNOWN_DETAIL}|{REASON}"))?,
            with: insensitive(r"\b(?:with|without|с|без)\b")?,
            medicine_before_verb: insensitive(r"\b(?:таблетк\w*|лекарств\w*|препарат\w*)\b")?,
            russian_singular_owner: insensitive(r"\bя\b")?,
            trailing_person: Regex::new(r"^\s+[А-ЯЁ][а-яё]+\b")?,
            implicit_subject: Regex::new(r"^\s*([А-Яа-яЁё][а-яё]+)\s+([а-яё][\w-]*)\b")?,
            adjective_ending: insensitive(r"(?:ую|юю|ая|яя|ое|ее)$")?,
            pronoun_object: insensitive(r"^\s+(?:it|them|его|е[её]|их)\b")?,
            reference: insensitive(&format!(
                r"({VERB}\s+)(его|е[её]|их|it|them|the\s+same\s+(?:medication|medicine|drug|pill))\b(?=\s*(?:$|[,.!?;]|\b(?:again|снова|повторно|at|в|сейчас|now|today|yesterday|сегодня|вчера|after|before|после|до|with|без|without)\b|{CLOCK}|{DOSE}|{relative}))"
            ))?,
            non_medical: insensitive(&format!(
                r"{VERB}\s+(?:(?:a|an|the|my|our)\s+)?(?:blood\s+pressure|temperature|pulse|blood\s+sugar|heart\s+rate|taxi|train|bus|flight|plane|subway|tram|такси|поезд|автобус|душ|решение|ванну|участие|звонок|вызов|it\s+easy|photo|picture|selfie|screenshot|exam|test|seat|look|breath|chance|risk|step|notes|care|shower|bath|decision|walk|break|part|call|nap)\b"
            ))?,
            zero_count: insensitive(r"\b0(?:[.,]0+)?\s+(?:[\w/-]+\s+)?(?:tablets?|pills?|таблетк\w*|капсул\w*)\b")?,
            drink: insensitive(r"\A(?:вы)?пил[аи]?\z")?,
            medicine_context: insensitive(r"\b(?:таблетк\w*|капсул\w*|лекарств\w*|препарат\w*|medicine|medications?|pills?|tablets?|capsules?)\b")?,
            owner_prefix: [
                r"^\s*(?:после|до|after|before)\s+[\w -]+\s+(?:я|I|мы|we)\s*$",
                &relative,
                CLOCK,
                DATE,
                &format!(r"\bчерез\s+{QUANTITY}\s+{UNIT}\b"),
                &format!(r"{UNKNOWN}\s+(?:таблетк\w*|лекарств\w*|препарат\w*|drugs?|pills?|tablets?|medicine)\b"),
                r"\b(?:после|до|after|before)\s+(?:еды|завтрака|обеда|ужина|food|breakfast|lunch|dinner)\b",
                GENERIC,
                r"\b(?:да|yes|also|definitely|certainly|actually|точно|также|действительно)\b",
            ].into_iter().map(insensitive).collect::<Result<_, _>>()?,
            quotes: Regex::new(QUOTED_NAME)?,
            clock: insensitive(CLOCK)?,
            approximate_context: insensitive(&format!(r"{APPROXIMATE}\s+(?:{QUANTITY}\s+)?{UNIT}\s+(?:after|before|после|до)\b"))?,
            phrase_noise: [
                r"\b(?:with\s+water|запил[аи]?\s+водой)\b",
                r"\b(?:после|до|с|after|before|with)\s+(?:еды|едой|завтрака|обеда|ужина|food|breakfast|lunch|dinner)\b",
            ].into_iter().map(insensitive).collect::<Result<_, _>>()?,
            phrase_ending: insensitive(&format!(r"{COMMA}|;|{UNKNOWN_DETAIL}|{REASON}|\b(?:после|до|запил[аи]?|after|before|with)\b"))?,
            purpose: insensitive(&format!(r"\b(?:от|for)\s+.*?(?=(?:{CLOCK})|(?:{DOSE})|$)"))?,
            alternative: insensitive(r"\b(?:or|или|либо)\b")?,
            named_count: insensitive(&format!(
                r"\b(?:{words})(?=\s+(?:{UNKNOWN}\s+)?(?:таблетк[ауи]|tablets?|pills?|capsules?|капсул[ауые]?)\b)"
            ))?,
            name_noise: [
                r"\b(?:as\s+(?:prescribed|directed)|orally|sublingually|buccally|rectally|натощак|перорально|сублингвально|ректально|внутрь|под\s+язык|on\s+an\s+empty\s+stomach)\b",
                &format!(r"{APPROXIMATE}\s+(?={DOSE})"),
                r"\b(?:twice|дважды)\b",
                r"^\s*(?:both|then|затем|потом)\b\s*",
                &relative,
                &format!(r"(?:{CLOCK})\s+час(?:а|ов)?\b"),
                CLOCK,
                DATE,
                &format!(r"(?:\bпо\s+)?(?:{DOSE})"),
                r"\(\s*\)|\[\s*\]",
                GENERIC,
                r"^\s*of\s+",
                r"\b(?:препарат(?:а|ы|ом)?|drugs?)\b",
                r"\bот\s+[\w-]+",
                UNKNOWN,
            ].into_iter().map(insensitive).collect::<Result<_, _>>()?,
            bare_dose_marker: insensitive(r"\b(?:доз[ауы]|дозировк[ауи]|dose|единиц[ауы](?:\s+измерения)?|unit)\b")?,
            numeric_name: Regex::new(r"\b\d+(?:[.,]\d+)?\b")?,
            name_prefix: insensitive(&format!(r"^\s*(?:{counts}|\d+(?:[.,]\d+)?)\s+|^\s*(?:a|an)\s+"))?,
            conjunction: insensitive(r"\b(?:и|and)\b")?,
            name: Regex::new(r"\A[\w-]+(?:[/’'][\w-]+)*(?:\s+[\w-]+(?:[/’'][\w-]+)*)*\z")?,
            numeric_designation: Regex::new(r"(?<=\w)[ -]+(?=\d)")?,
            russian_consonant: Regex::new(r"\A[а-яё-]+[бвгджзклмнпрстфхцчшщ]\z")?,
            russian_vowel: Regex::new(r"\A[а-яё-]+[ая]\z")?,
        })
    }
}

fn rules(text: &str) -> Result<&'static Rules, DoseTextError> {
    static RULES: OnceLock<Option<Rules>> = OnceLock::new();
    if text.len() > MAX_TEXT_BYTES {
        return Err(DoseTextError::InputTooLarge);
    }
    RULES
        .get_or_init(|| Rules::new().ok())
        .as_ref()
        .ok_or(DoseTextError::Invalid)
}

/// Check the subject of one already isolated intake clause.
pub fn owner_assertion(clause: &str) -> Result<bool, DoseTextError> {
    let rules = rules(clause)?;
    let head = &clause[..rules
        .qualifiers
        .find(clause)?
        .map_or(clause.len(), |part| part.start())];
    let Some(verb) = rules.verb.find(head)? else {
        return Ok(false);
    };
    let prefix = &clause[..verb.start()];
    let suffix = &clause[verb.end()..];
    let predicate_end = rules
        .with
        .find(&head[verb.end()..])?
        .map_or(head.len(), |part| verb.end() + part.start());
    let predicate = &head[..predicate_end];
    let subject_scope = if rules.owner.is_match(prefix)? {
        prefix
    } else {
        predicate
    };

    if rules.negative.is_match(predicate)?
        || rules.zero_count.is_match(predicate)?
        || rules.other.is_match(subject_scope)?
        || rules.non_medical.is_match(clause)?
        || (rules.pronoun_object.is_match(suffix)? && !rules.reference.is_match(clause)?)
        || (rules.drink.is_match(verb.as_str())? && !rules.medicine_context.is_match(clause)?)
        || (rules.medicine_before_verb.is_match(prefix)?
            && !rules.russian_singular_owner.is_match(prefix)?
            && rules.trailing_person.is_match(suffix)?)
    {
        return Ok(false);
    }
    if !rules.owner.is_match(prefix)? && ambiguous_implicit_subject(clause, rules)? {
        return Ok(false);
    }
    let mut prefix = prefix.to_owned();
    for fragment in &rules.owner_prefix {
        replace(&mut prefix, fragment, "")?;
    }
    Ok(prefix.trim_matches([' ', ',', ';', ':']).is_empty())
}

fn ambiguous_implicit_subject(clause: &str, rules: &Rules) -> Result<bool, DoseTextError> {
    let mut names = literal_names(clause)?;
    for quoted in rules.quotes.find_iter(clause) {
        let value = quoted?.as_str();
        names.remove(&casefold(unquote(value)));
    }
    let Some(name) = names.first().filter(|_| names.len() == 1) else {
        return Ok(false);
    };
    let Some(subject) = rules.implicit_subject.captures(name.as_str())? else {
        return Ok(false);
    };
    Ok(!rules.generic_name.is_match(&subject[1])?
        && !rules.generic_name.is_match(&subject[2])?
        && !rules.adjective_ending.is_match(&subject[1])?
        && !matches!(
            casefold(&subject[2]).as_str(),
            "форте" | "кардио" | "макс" | "плюс" | "экспресс"
        ))
}

/// Return the names the clause itself mentions, preserving quoted product names.
pub fn literal_names(clause: &str) -> Result<BTreeSet<String>, DoseTextError> {
    let rules = rules(clause)?;
    let mut phrase = clause.to_owned();
    let folded = casefold(clause);
    let mut quote_key = String::from("quotedmedication");
    while folded.contains(&quote_key) {
        quote_key.push('x');
    }
    let mut quoted = BTreeMap::new();
    replace(&mut phrase, &rules.quotes, |matched: &Captures<'_, str>| {
        let key = format!("{quote_key}{}", quoted.len());
        quoted.insert(key.clone(), casefold(unquote(&matched[0])));
        key
    })?;
    let mut phrase = medication_phrase(phrase, rules)?;
    for pattern in &rules.name_noise[..4] {
        replace(&mut phrase, pattern, "")?;
    }
    replace(&mut phrase, &rules.named_count, "")?;
    if rules.alternative.is_match(&phrase)? {
        return Ok(BTreeSet::new());
    }
    for pattern in &rules.name_noise[4..] {
        replace(&mut phrase, pattern, "")?;
    }
    if rules.bare_dose_marker.is_match(clause)? {
        replace(&mut phrase, &rules.numeric_name, "")?;
    }
    replace(&mut phrase, &rules.name_prefix, "")?;
    let phrase = phrase.trim_matches([' ', '.', '!', ';', ':', '(', ')', '[', ']']);
    let mut names = BTreeSet::new();
    for term in rules.conjunction.split(phrase) {
        let term = term?.trim();
        if rules.name.is_match(term)? {
            let term = casefold(term);
            names.insert(quoted.remove(&term).unwrap_or(term));
        }
    }
    Ok(names)
}

fn medication_phrase(mut clause: String, rules: &Rules) -> Result<String, DoseTextError> {
    if let Some(fragment) = rules.approximate_context.find(&clause)?
        && rules.clock.is_match(&clause[..fragment.start()])?
    {
        clause.truncate(fragment.start());
    }
    for pattern in &rules.phrase_noise {
        replace(&mut clause, pattern, "")?;
    }
    let Some(verb) = rules.verb.find(&clause)? else {
        return Ok(String::new());
    };
    let tail = &clause[verb.end()..];
    let mut tail = tail[..rules
        .phrase_ending
        .find(tail)?
        .map_or(tail.len(), |part| part.start())]
        .to_owned();
    replace(&mut tail, &rules.purpose, "")?;
    Ok(tail)
}

fn unquote(value: &str) -> &str {
    value
        .trim_start_matches(['«', '"'])
        .trim_end_matches(['»', '"'])
}

fn casefold(text: &str) -> String {
    text.to_lowercase()
        .replace('ſ', "s")
        .replace('ß', "ss")
        .replace('ς', "σ")
}

/// Match an offered product name to a literal product name or its accepted inflection.
pub fn name_matches(expected: &str, literal: &str) -> Result<bool, DoseTextError> {
    let rules = rules(expected)?;
    let _ = self::rules(literal)?;
    let mut expected = casefold(expected);
    let mut literal = casefold(literal);
    replace(&mut expected, &rules.numeric_designation, "-")?;
    replace(&mut literal, &rules.numeric_designation, "-")?;
    let expected = expected.split_whitespace().collect::<Vec<_>>();
    let literal = literal.split_whitespace().collect::<Vec<_>>();
    if expected.len() != literal.len() {
        return Ok(false);
    }
    for (word, given) in expected.into_iter().zip(literal) {
        if !word_matches(word, given, rules)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn word_matches(word: &str, literal: &str, rules: &Rules) -> Result<bool, DoseTextError> {
    if word == literal {
        return Ok(true);
    }
    if rules.russian_consonant.is_match(word)?
        && literal
            .strip_prefix(word)
            .is_some_and(|end| matches!(end, "а" | "у" | "ом" | "е"))
    {
        return Ok(true);
    }
    if rules.russian_vowel.is_match(word)? {
        let (ending, alternatives) = if word.ends_with('а') {
            ('а', ["у", "ы", "е", "ой"])
        } else {
            ('я', ["ю", "и", "е", "ей"])
        };
        if word
            .strip_suffix(ending)
            .and_then(|stem| literal.strip_prefix(stem))
            .is_some_and(|end| alternatives.contains(&end))
        {
            return Ok(true);
        }
    }
    Ok(word
        .strip_suffix("ая")
        .and_then(|stem| literal.strip_prefix(stem))
        .is_some_and(|end| matches!(end, "ую" | "ой" | "ою"))
        || word
            .strip_suffix("яя")
            .and_then(|stem| literal.strip_prefix(stem))
            .is_some_and(|end| matches!(end, "юю" | "ей" | "ею")))
}
