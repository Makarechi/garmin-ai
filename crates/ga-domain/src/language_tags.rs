use std::collections::BTreeSet;

const GRANDFATHERED: &[&str] = &[
    "en-GB-oed",
    "i-ami",
    "i-bnn",
    "i-default",
    "i-enochian",
    "i-hak",
    "i-klingon",
    "i-lux",
    "i-mingo",
    "i-navajo",
    "i-pwn",
    "i-tao",
    "i-tay",
    "i-tsu",
    "sgn-BE-FR",
    "sgn-BE-NL",
    "sgn-CH-DE",
    "art-lojban",
    "cel-gaulish",
    "no-bok",
    "no-nyn",
    "zh-guoyu",
    "zh-hakka",
    "zh-min",
    "zh-min-nan",
    "zh-xiang",
];

/// Validate the project's language tag syntax and apply conventional casing.
pub fn normalize_language_tag(value: &str) -> Option<String> {
    if value.len() > 255 {
        return None;
    }
    if let Some(tag) = GRANDFATHERED
        .iter()
        .find(|tag| tag.eq_ignore_ascii_case(value))
    {
        return Some((*tag).to_owned());
    }
    let lowered = value.to_ascii_lowercase();
    let mut parts = lowered.split('-').peekable();
    if lowered.split('-').any(|part| {
        !(1..=8).contains(&part.len()) || !part.bytes().all(|byte| byte.is_ascii_alphanumeric())
    }) {
        return None;
    }
    let language = parts.next()?;
    if language == "x" {
        parts.next()?;
        return Some(lowered);
    }
    if !(2..=8).contains(&language.len()) || !letters(language) {
        return None;
    }
    let mut normalized = vec![language.to_owned()];
    if language.len() <= 3 {
        for _ in 0..3 {
            let Some(extlang) = parts.next_if(|part| part.len() == 3 && letters(part)) else {
                break;
            };
            normalized.push(extlang.to_owned());
        }
    }
    if let Some(script) = parts.next_if(|part| part.len() == 4 && letters(part)) {
        let mut script = script.to_owned();
        script.get_mut(..1)?.make_ascii_uppercase();
        normalized.push(script);
    }
    if let Some(region) = parts.next_if(|part| {
        (part.len() == 2 && letters(part))
            || (part.len() == 3 && part.bytes().all(|byte| byte.is_ascii_digit()))
    }) {
        normalized.push(region.to_ascii_uppercase());
    }
    let mut variants = BTreeSet::new();
    while let Some(variant) = parts.next_if(|part| {
        (5..=8).contains(&part.len()) || (part.len() == 4 && part.as_bytes()[0].is_ascii_digit())
    }) {
        if !variants.insert(variant) {
            return None;
        }
        normalized.push(variant.to_owned());
    }
    let mut extensions = BTreeSet::new();
    while let Some(singleton) = parts.next_if(|part| part.len() == 1 && *part != "x") {
        if !extensions.insert(singleton) {
            return None;
        }
        normalized.push(singleton.to_owned());
        let first = parts.next_if(|part| part.len() >= 2)?;
        normalized.push(first.to_owned());
        while let Some(extension) = parts.next_if(|part| part.len() >= 2) {
            normalized.push(extension.to_owned());
        }
    }
    if parts.next_if_eq(&"x").is_some() {
        normalized.push("x".to_owned());
        normalized.push(parts.next()?.to_owned());
        normalized.extend(parts.map(str::to_owned));
    } else if parts.next().is_some() {
        return None;
    }
    Some(normalized.join("-"))
}

fn letters(part: &str) -> bool {
    part.bytes().all(|byte| byte.is_ascii_alphabetic())
}
