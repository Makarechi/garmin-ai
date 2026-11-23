use num_bigint::BigInt;
use serde_json::{Number, Value, json};

use super::{ChatFormError, answer, casefold, unicode};
use crate::{
    event_payload::python_whitespace,
    event_schema::{check_value_limits, custom::ExactNumber, entry_validator},
    file_import::{ImportFileError, decimal::Decimal, json::read_chat_value},
    legacy_json::{self, Utf8Layout},
    metric_contract::decimal_value,
};

pub(super) fn json_text(value: &Value, sorted: bool) -> Result<String, ChatFormError> {
    let input = serde_json::to_vec(value).map_err(|_| ChatFormError::Invalid)?;
    let bytes = legacy_json::encode(
        &input,
        if sorted {
            Utf8Layout::Archive
        } else {
            Utf8Layout::Ordered
        },
        input.len(),
    )
    .map_err(|_| ChatFormError::Invalid)?;
    String::from_utf8(bytes).map_err(|_| ChatFormError::Invalid)
}

pub(super) fn python_string(value: &Value) -> Result<String, ChatFormError> {
    Ok(match value {
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        Value::Number(_) => json_text(value, false)?,
        Value::Array(items) => format!(
            "[{}]",
            items
                .iter()
                .map(python_repr)
                .collect::<Result<Vec<_>, _>>()?
                .join(", ")
        ),
        Value::Object(items) => format!(
            "{{{}}}",
            items
                .iter()
                .map(|(key, value)| Ok(format!(
                    "{}: {}",
                    python_repr(&json!(key))?,
                    python_repr(value)?
                )))
                .collect::<Result<Vec<_>, ChatFormError>>()?
                .join(", ")
        ),
    })
}

fn python_repr(value: &Value) -> Result<String, ChatFormError> {
    let Value::String(text) = value else {
        return python_string(value);
    };
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::new();
    result.push(quote);
    for character in text.chars() {
        match character {
            '\\' => result.push_str("\\\\"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            character if character == quote => {
                result.push('\\');
                result.push(character);
            }
            character if !printable(character) => {
                use std::fmt::Write;
                let code = u32::from(character);
                if code <= 0xff {
                    write!(result, "\\x{code:02x}")
                } else if code <= 0xffff {
                    write!(result, "\\u{code:04x}")
                } else {
                    write!(result, "\\U{code:08x}")
                }
                .map_err(|_| ChatFormError::Invalid)?;
            }
            character => result.push(character),
        }
    }
    result.push(quote);
    Ok(result)
}

fn printable(character: char) -> bool {
    let code = u32::from(character);
    let ranges = unicode::NONPRINTABLE;
    let index = ranges.partition_point(|&(_, last)| last < code);
    ranges.get(index).is_none_or(|&(first, _)| first > code)
}

/// Produce the exact labels accepted for enumerated chat answers.
pub fn chat_choice_labels(options: &[Value]) -> Result<Vec<String>, ChatFormError> {
    let mut labels = Vec::with_capacity(options.len());
    for (index, option) in options.iter().enumerate() {
        let label = match option {
            Value::String(text) if text.is_empty() => "/empty".to_owned(),
            Value::String(text) if text.trim_matches(python_whitespace).is_empty() => {
                format!("{}: {}", index + 1, json_text(option, false)?)
            }
            _ => python_string(option)?,
        };
        labels.push(
            if label
                .trim_start_matches(python_whitespace)
                .starts_with(['/', '='])
            {
                format!("={label}")
            } else {
                label
            },
        );
    }
    let distinct = labels.iter().collect::<std::collections::BTreeSet<_>>();
    if distinct.len() == labels.len() {
        return Ok(labels);
    }
    options
        .iter()
        .enumerate()
        .map(|(index, option)| Ok(format!("{}: {}", index + 1, json_text(option, true)?)))
        .collect()
}

pub(super) fn storable(field: &Value, value: &Value) -> Result<bool, ChatFormError> {
    if check_value_limits(&json!({"value":value}), 0).is_err() {
        return Ok(false);
    }
    if let Value::String(text) = value {
        let length = text.chars().count() as u64;
        if field["min_length"].as_u64().is_some_and(|min| length < min)
            || field["max_length"].as_u64().is_some_and(|max| length > max)
        {
            return Ok(false);
        }
    }
    if field["validation_schema"].is_null() {
        return Ok(true);
    }
    Ok(entry_validator(&field["validation_schema"])
        .map_err(|_| ChatFormError::Invalid)?
        .is_valid(value))
}

/// Parse one answer using the pinned field projection, including literal commands.
pub fn parse_chat_form_value(
    field: &Value,
    text: &str,
    locale: &str,
) -> Result<Value, ChatFormError> {
    let kind = field["input"].as_str().ok_or(ChatFormError::Invalid)?;
    let (text, literal) = if kind == "text" && text == "=/empty" {
        ("", true)
    } else if kind == "text"
        && text.ends_with("/empty")
        && text[..text.len() - 6].len() >= 2
        && text[..text.len() - 6].bytes().all(|c| c == b'=')
    {
        (&text[2..], true)
    } else if matches!(kind, "text" | "choice") && text.starts_with('=') {
        (&text[1..], true)
    } else {
        (text, false)
    };
    if text == "-" && kind == "text" {
        return Ok(json!("-"));
    }
    if text == "/skip" && !literal {
        return if field["required"] == true {
            Err(answer(
                locale,
                "Обязательное поле нельзя пропустить",
                "A required field cannot be skipped",
            ))
        } else {
            Ok(Value::Null)
        };
    }
    let value = match kind {
        "text" => {
            if text.starts_with('/') && !literal {
                return Err(answer(
                    locale,
                    "Начните буквальное значение с =",
                    "Prefix a literal value with =",
                ));
            }
            if text.contains('\0') {
                return Err(answer(
                    locale,
                    "Текст содержит неподдерживаемые символы",
                    "Text contains unsupported characters",
                ));
            }
            let length = text.chars().count() as u64;
            if field["min_length"].as_u64().is_some_and(|min| length < min)
                || field["max_length"].as_u64().is_some_and(|max| length > max)
            {
                return Err(answer(
                    locale,
                    "Укажите текст допустимой длины",
                    "Enter text within the allowed length",
                ));
            }
            return Ok(json!(text));
        }
        "integer" => {
            let digits = text.trim_start_matches('-');
            if digits.is_empty()
                || !digits.chars().all(|c| {
                    decimal_value(c).is_some()
                        || unicode::EXTRA_DIGITS.binary_search(&u32::from(c)).is_ok()
                })
            {
                return Err(answer(locale, "Нужно целое число", "Enter a whole number"));
            }
            let mut normalized = String::with_capacity(text.len());
            for character in text.chars() {
                if character == '-' {
                    normalized.push('-');
                } else {
                    normalized.push(char::from(
                        b'0' + decimal_value(character).ok_or(ChatFormError::Invalid)?,
                    ));
                }
            }
            if normalized.trim_start_matches('-').len() > 4300 {
                return Err(ChatFormError::Invalid);
            }
            let integer = normalized
                .parse::<BigInt>()
                .map_err(|_| ChatFormError::Invalid)?;
            Value::Number(
                integer
                    .to_string()
                    .parse()
                    .map_err(|_| ChatFormError::Invalid)?,
            )
        }
        "number" => {
            if text.contains(',') && message_english(locale) {
                return Err(answer(
                    locale,
                    "Укажите число с точкой",
                    "Use a decimal point",
                ));
            }
            let token = decimal_token(&text.replace(',', "."))
                .ok_or_else(|| answer(locale, "Нужно число", "Enter a number"))?;
            if nonfinite(&token) {
                return Err(answer(
                    locale,
                    "Нужно конечное число",
                    "Enter a finite number",
                ));
            }
            let decimal = chat_decimal(&token)
                .ok_or_else(|| answer(locale, "Нужно число", "Enter a number"))?;
            if decimal.is_integral() {
                if decimal.adjusted(0).is_none_or(|adjusted| adjusted >= 4096) {
                    return Err(answer(
                        locale,
                        "Число слишком длинное",
                        "Number is too long",
                    ));
                }
                let integer = decimal.integer_token().ok_or(ChatFormError::Invalid)?;
                Value::Number(integer.parse().map_err(|_| ChatFormError::Invalid)?)
            } else {
                exact_float(&token).map_err(|_| {
                    answer(
                        locale,
                        "Слишком много знаков для точной записи",
                        "Too many digits to save exactly",
                    )
                })?
            }
        }
        "boolean" => {
            return match casefold(text).as_str() {
                "да" | "yes" | "true" => Ok(Value::Bool(true)),
                "нет" | "no" | "false" => Ok(Value::Bool(false)),
                _ => Err(answer(locale, "Ответьте «да» или «нет»", "Reply yes or no")),
            };
        }
        "choice" => {
            let options = field["options"].as_array().ok_or(ChatFormError::Invalid)?;
            let labels = chat_choice_labels(options)?;
            let target = if literal && text.is_empty() {
                "=/empty".into()
            } else if literal {
                format!("={text}")
            } else {
                text.to_owned()
            };
            let mut matches = labels
                .iter()
                .enumerate()
                .filter(|(_, label)| **label == target)
                .map(|(index, _)| index)
                .collect::<Vec<_>>();
            if matches.len() != 1 {
                let target = casefold(&target);
                matches = labels
                    .iter()
                    .enumerate()
                    .filter(|(_, label)| casefold(label) == target)
                    .map(|(index, _)| index)
                    .collect();
            }
            if matches.len() != 1 {
                return Err(answer(
                    locale,
                    "Выберите один из перечисленных вариантов",
                    "Choose one of the listed options",
                ));
            }
            let selected = &options[matches[0]];
            if !storable(field, selected)? {
                return Err(answer(
                    locale,
                    "Этот вариант не соответствует схеме поля. Выберите другой или пропустите поле.",
                    "This option does not satisfy the field schema. Choose another or skip the field.",
                ));
            }
            return Ok(selected.clone());
        }
        "json" => {
            return match read_chat_value(text, chat_json_number) {
                Ok(Ok(value)) => Ok(value),
                Ok(Err(_)) => Err(answer(
                    locale,
                    "JSON содержит неподдерживаемые символы",
                    "JSON contains unsupported characters",
                )),
                Err(ImportFileError::JsonDecimalRange) => Err(ChatFormError::DecimalRange),
                Err(ImportFileError::JsonPrecision) => Err(answer(
                    locale,
                    "Слишком много знаков для точной записи",
                    "Too many digits to save exactly",
                )),
                Err(_) => Err(ChatFormError::Invalid),
            };
        }
        _ => {
            return Err(answer(
                locale,
                "Тип поля не поддерживается в чате",
                "This field type is unavailable in chat",
            ));
        }
    };
    let number = ExactNumber::read(&value).ok_or(ChatFormError::Invalid)?;
    if let Some(min) = ExactNumber::read(&field["minimum"])
        && (number < min || field["exclusive_minimum"] == true && number == min)
    {
        return Err(if field["exclusive_minimum"] == true {
            answer(
                locale,
                "Значение должно быть выше минимума",
                "Value must exceed the minimum",
            )
        } else {
            answer(
                locale,
                "Значение ниже минимума",
                "Value is below the minimum",
            )
        });
    }
    if let Some(max) = ExactNumber::read(&field["maximum"])
        && (number > max || field["exclusive_maximum"] == true && number == max)
    {
        return Err(if field["exclusive_maximum"] == true {
            answer(
                locale,
                "Значение должно быть ниже максимума",
                "Value must be below the maximum",
            )
        } else {
            answer(
                locale,
                "Значение выше максимума",
                "Value is above the maximum",
            )
        });
    }
    Ok(value)
}

fn message_english(locale: &str) -> bool {
    !locale
        .split('-')
        .next()
        .is_some_and(|code| code.eq_ignore_ascii_case("ru"))
}

fn nonfinite(token: &str) -> bool {
    let token = token.trim_start_matches(['+', '-']).to_ascii_lowercase();
    matches!(token.as_str(), "inf" | "infinity")
        || token
            .strip_prefix("snan")
            .or_else(|| token.strip_prefix("nan"))
            .is_some_and(|tail| tail.bytes().all(|byte| byte.is_ascii_digit()))
}

fn decimal_token(text: &str) -> Option<String> {
    let mut token = String::with_capacity(text.len());
    for character in text
        .trim_matches(python_whitespace)
        .chars()
        .filter(|&c| c != '_')
    {
        match decimal_value(character) {
            Some(digit) => token.push(char::from(b'0' + digit)),
            None => token.push(character),
        }
    }
    if nonfinite(&token) {
        return Some(token);
    }
    let unsigned = token.strip_prefix(['+', '-']).unwrap_or(&token);
    let (coefficient, exponent) = unsigned
        .split_once(['e', 'E'])
        .map_or((unsigned, None), |(c, e)| (c, Some(e)));
    if let Some(exponent) = exponent {
        let digits = exponent.strip_prefix(['+', '-']).unwrap_or(exponent);
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
    }
    let mut decimal = false;
    let mut digits = 0;
    for byte in coefficient.bytes() {
        if byte.is_ascii_digit() {
            digits += 1;
        } else if byte == b'.' && !decimal {
            decimal = true;
        } else {
            return None;
        }
    }
    (digits > 0).then_some(token)
}

fn exact_float(token: &str) -> Result<Value, ImportFileError> {
    let value = token
        .parse::<f64>()
        .map_err(|_| ImportFileError::InvalidFormat)?;
    if !value.is_finite() {
        return Err(ImportFileError::JsonFloatOverflow);
    }
    let mut bytes = Vec::new();
    legacy_json::write_float(&mut bytes, value).map_err(|_| ImportFileError::JsonFloatOverflow)?;
    let formatted = std::str::from_utf8(&bytes).map_err(|_| ImportFileError::InvalidFormat)?;
    if Decimal::new(token) != Decimal::new(formatted) {
        return Err(ImportFileError::JsonPrecision);
    }
    Ok(Value::Number(
        formatted
            .parse()
            .map_err(|_| ImportFileError::InvalidFormat)?,
    ))
}

fn chat_json_number(bytes: &[u8]) -> Result<Number, ImportFileError> {
    let token = std::str::from_utf8(bytes).map_err(|_| ImportFileError::InvalidFormat)?;
    if nonfinite(token) {
        return Err(ImportFileError::JsonNonfinite);
    }
    if token == "-0" {
        return Ok(Number::from(0));
    }
    if !token.contains(['.', 'e', 'E']) {
        return token.parse().map_err(|_| ImportFileError::InvalidFormat);
    }
    let decimal = chat_decimal(token).ok_or(ImportFileError::JsonDecimalRange)?;
    if decimal.adjusted(0).is_some_and(|adjusted| adjusted > 1000) {
        return Err(ImportFileError::JsonFloatOverflow);
    }
    if let Some(integer) = decimal.integer_token() {
        return integer.parse().map_err(|_| ImportFileError::InvalidFormat);
    }
    exact_float(token)?
        .as_number()
        .cloned()
        .ok_or(ImportFileError::InvalidFormat)
}

fn chat_decimal(token: &str) -> Option<Decimal> {
    let unsigned = token.strip_prefix(['+', '-']).unwrap_or(token);
    let (coefficient, exponent) = unsigned.split_once(['e', 'E']).unwrap_or((unsigned, "0"));
    let fraction = coefficient
        .split_once('.')
        .map_or(0, |(_, fraction)| fraction.len());
    let exponent = exponent
        .parse::<i64>()
        .ok()?
        .checked_sub(i64::try_from(fraction).ok()?)?;
    let significant = coefficient
        .bytes()
        .filter(|&byte| byte != b'.')
        .skip_while(|&byte| byte == b'0')
        .count();
    let adjusted = if significant == 0 {
        exponent
    } else {
        exponent.checked_add(i64::try_from(significant).ok()? - 1)?
    };
    if exponent < -1_999_999_999_999_999_997 || adjusted > 999_999_999_999_999_999 {
        return None;
    }
    Decimal::new(token)
}
