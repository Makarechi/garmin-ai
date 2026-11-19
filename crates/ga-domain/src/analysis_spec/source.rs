use std::{fmt::Write, iter::Peekable, str::Chars};

use super::InvalidAnalysisSpec;

/// Preserve Python JSON's UTF-16 code units, including individual surrogate escapes.
pub(super) fn canonical_pair(text: &str) -> Result<String, InvalidAnalysisSpec> {
    let mut chars = text.chars().peekable();
    punctuation(&mut chars, '[')?;
    let account = item(&mut chars)?;
    punctuation(&mut chars, ',')?;
    let device = item(&mut chars)?;
    punctuation(&mut chars, ']')?;
    whitespace(&mut chars);
    if chars.next().is_some() {
        return Err(InvalidAnalysisSpec::Validation);
    }
    Ok(format!("[{account},{device}]"))
}

fn whitespace(chars: &mut Peekable<Chars<'_>>) {
    while chars
        .peek()
        .is_some_and(|value| matches!(value, ' ' | '\t' | '\r' | '\n'))
    {
        chars.next();
    }
}

fn punctuation(chars: &mut Peekable<Chars<'_>>, expected: char) -> Result<(), InvalidAnalysisSpec> {
    whitespace(chars);
    if chars.next() == Some(expected) {
        Ok(())
    } else {
        Err(InvalidAnalysisSpec::Validation)
    }
}

fn item(chars: &mut Peekable<Chars<'_>>) -> Result<String, InvalidAnalysisSpec> {
    whitespace(chars);
    if chars.peek() == Some(&'n') {
        for expected in "null".chars() {
            if chars.next() != Some(expected) {
                return Err(InvalidAnalysisSpec::Validation);
            }
        }
        return Ok("null".to_owned());
    }
    if chars.next() != Some('"') {
        return Err(InvalidAnalysisSpec::Validation);
    }
    let mut output = String::from("\"");
    loop {
        match chars.next().ok_or(InvalidAnalysisSpec::Validation)? {
            '"' => break,
            '\\' => {
                let unit = match chars.next().ok_or(InvalidAnalysisSpec::Validation)? {
                    '"' => u16::from(b'"'),
                    '\\' => u16::from(b'\\'),
                    '/' => u16::from(b'/'),
                    'b' => 8,
                    'f' => 12,
                    'n' => 10,
                    'r' => 13,
                    't' => 9,
                    'u' => {
                        let mut value = 0;
                        for _ in 0..4 {
                            let hex = chars
                                .next()
                                .and_then(|value| value.to_digit(16))
                                .ok_or(InvalidAnalysisSpec::Validation)?;
                            value = value * 16 + hex as u16;
                        }
                        value
                    }
                    _ => return Err(InvalidAnalysisSpec::Validation),
                };
                write_unit(&mut output, unit)?;
            }
            value if value < '\u{20}' => return Err(InvalidAnalysisSpec::Validation),
            value => {
                for unit in value.encode_utf16(&mut [0; 2]) {
                    write_unit(&mut output, *unit)?;
                }
            }
        }
    }
    output.push('"');
    Ok(output)
}

fn write_unit(output: &mut String, unit: u16) -> Result<(), InvalidAnalysisSpec> {
    match unit {
        34 => output.push_str("\\\""),
        92 => output.push_str("\\\\"),
        8 => output.push_str("\\b"),
        12 => output.push_str("\\f"),
        10 => output.push_str("\\n"),
        13 => output.push_str("\\r"),
        9 => output.push_str("\\t"),
        32..=126 => output.push(char::from(unit as u8)),
        _ => write!(output, "\\u{unit:04x}").map_err(|_| InvalidAnalysisSpec::Validation)?,
    }
    Ok(())
}
