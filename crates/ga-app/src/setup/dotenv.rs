use std::collections::{BTreeMap, BTreeSet};

use super::{INVALID_SETTINGS, ValueMap};

pub(super) struct SetupFile<'a> {
    records: Vec<(Option<&'a str>, &'a str)>,
    pub(super) values: ValueMap,
}

impl<'a> SetupFile<'a> {
    pub(super) fn parse(input: &'a str) -> Result<Self, &'static str> {
        let mut remaining = input;
        let mut records = Vec::new();
        let mut values = BTreeMap::new();
        while !remaining.is_empty() {
            let first_line = line(remaining);
            let binding = first_line.trim_start_matches('\u{feff}').trim_start();
            let binding = if let Some(tail) = binding.strip_prefix("export ") {
                tail.trim_start()
            } else {
                binding
            };
            let (key, value, width) = if binding.is_empty() || binding.starts_with('#') {
                (None, None, first_line.len())
            } else if let Some((key, value)) = binding.split_once('=') {
                let key = key.trim_end();
                if !environment_name(key) {
                    return Err(INVALID_SETTINGS);
                }
                let offset = first_line.len() - value.len();
                let value = remaining[offset..].trim_start_matches([' ', '\t']);
                let (decoded, consumed) = parse_value(value, &values)?;
                let width = remaining.len() - value.len() + consumed;
                (Some(key), Some(decoded), width)
            } else {
                let key = binding.trim();
                if !environment_name(key) {
                    return Err(INVALID_SETTINGS);
                }
                (Some(key), None, first_line.len())
            };
            if let (Some(key), Some(value)) = (key, value) {
                values.insert(key.to_owned(), value);
            }
            records.push((key, &remaining[..width]));
            remaining = &remaining[width..];
        }
        Ok(Self { records, values })
    }

    pub(super) fn render(&self, values: &ValueMap) -> String {
        let changed: BTreeMap<_, _> = values
            .iter()
            .filter(|(name, value)| self.values.get(*name) != Some(*value))
            .map(|(name, value)| (name.as_str(), value.as_str()))
            .collect();
        let mut missing: BTreeSet<_> = changed.keys().copied().collect();
        let mut output = String::new();
        for (name, original) in &self.records {
            if let Some(name) = name
                && let Some(value) = changed.get(name)
            {
                output.push_str(&binding(name, value));
                missing.remove(name);
            } else {
                output.push_str(original);
            }
        }
        for name in missing {
            if !output.is_empty() && !output.ends_with('\n') {
                output.push('\n');
            }
            output.push_str(&binding(name, changed[name]));
        }
        output
    }

    pub(super) fn replace_binding(&self, name: &str, value: &str) -> String {
        let assignment = binding(name, value);
        let mut output = String::new();
        let mut found = false;
        for (key, original) in &self.records {
            if key.is_some_and(|key| key.eq_ignore_ascii_case(name)) {
                output.push_str(&assignment);
                found = true;
            } else {
                output.push_str(original);
            }
        }
        if !found {
            if !output.is_empty() && !output.ends_with('\n') {
                output.push('\n');
            }
            output.push_str(&assignment);
        }
        output
    }
}

fn binding(name: &str, value: &str) -> String {
    if value.contains('\'') {
        let escaped = value
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('$', "\\$")
            .replace('\n', "\\n");
        format!("{name}=\"{escaped}\"\n")
    } else {
        format!("{name}='{}'\n", value.replace('\\', "\\\\"))
    }
}

fn environment_name(name: &str) -> bool {
    let mut bytes = name.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || byte == b'_')
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.'))
}

fn line(input: &str) -> &str {
    &input[..input.find('\n').map_or(input.len(), |offset| offset + 1)]
}

fn parse_value(input: &str, current: &ValueMap) -> Result<(String, usize), &'static str> {
    let Some(quote @ ('\'' | '"')) = input.chars().next() else {
        let raw = line(input);
        let comment = raw
            .char_indices()
            .find(|(offset, character)| *character == '#' && raw[..*offset].ends_with([' ', '\t']))
            .map_or(raw.len(), |(offset, _)| offset);
        return Ok((
            interpolate(raw[..comment].trim_end(), current, &[])?,
            raw.len(),
        ));
    };
    let mut value = String::new();
    let mut literal_dollars = Vec::new();
    let mut chars = input.char_indices();
    chars.next();
    while let Some((offset, character)) = chars.next() {
        if character == quote {
            let suffix = line(&input[offset + 1..]);
            let tail = suffix.trim();
            if !tail.is_empty() && !tail.starts_with('#') {
                return Err(INVALID_SETTINGS);
            }
            let value = if quote == '\'' {
                value
            } else {
                interpolate(&value, current, &literal_dollars)?
            };
            return Ok((value, offset + 1 + suffix.len()));
        }
        if character == '\\'
            && let Some((_, escaped)) = chars.next()
        {
            match (quote, escaped) {
                (_, '\\') | ('\'', '\'') | ('"', '"') => value.push(escaped),
                ('"', '$') => {
                    literal_dollars.push(value.len());
                    value.push('$');
                }
                ('"', 'n') => value.push('\n'),
                ('"', 'r') => value.push('\r'),
                ('"', 't') => value.push('\t'),
                _ => value.extend(['\\', escaped]),
            }
        } else {
            value.push(character);
        }
    }
    Err(INVALID_SETTINGS)
}

fn interpolate(
    value: &str,
    current: &ValueMap,
    literal_dollars: &[usize],
) -> Result<String, &'static str> {
    let mut tail = value;
    let mut output = String::new();
    while let Some((prefix, reference)) = tail.split_once("${") {
        output.push_str(prefix);
        let dollar = value.len() - tail.len() + prefix.len();
        if literal_dollars.binary_search(&dollar).is_ok() {
            output.push_str("${");
            tail = reference;
            continue;
        }
        let (name, rest) = reference.split_once('}').ok_or(INVALID_SETTINGS)?;
        let (name, default) = name.split_once(":-").unwrap_or((name, ""));
        if !environment_name(name) {
            return Err(INVALID_SETTINGS);
        }
        if let Some(value) = current.get(name) {
            output.push_str(value);
        } else if let Ok(value) = std::env::var(name) {
            output.push_str(&value);
        } else {
            output.push_str(default);
        }
        tail = rest;
    }
    output.push_str(tail);
    Ok(output)
}
