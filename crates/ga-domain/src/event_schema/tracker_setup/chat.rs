use serde_json::{Value, json};

use super::{Field, TrackerSetupError};
use crate::{event_payload::python_whitespace, metric_contract::decimal_value};

/// Parse the explicit field syntax accepted by the durable tracker chat setup.
pub fn parse_tracker_chat_field(text: &str, key: &str) -> Result<Value, TrackerSetupError> {
    use TrackerSetupError::Invalid;
    let (label, specification) = text.split_once('|').ok_or(Invalid)?;
    let label = label.trim_matches(python_whitespace);
    let specification = specification.trim_matches(python_whitespace);
    if label.is_empty() || specification.contains('|') {
        return Err(Invalid);
    }
    let boundary = specification
        .find(python_whitespace)
        .unwrap_or(specification.len());
    // Long s is the case-fold variant that can occur in the accepted type names.
    let kind = specification[..boundary].to_lowercase().replace('ſ', "s");
    let argument = specification[boundary..].trim_matches(python_whitespace);
    let mut field = json!({"key":key,"label":label});
    match kind.as_str() {
        "текст" | "text" if argument.is_empty() => field["kind"] = json!("text"),
        "да/нет" | "yes/no" | "boolean" if argument.is_empty() => {
            field["kind"] = json!("boolean");
        }
        "выбор" | "choice" => {
            field["kind"] = json!("choice");
            field["options"] = argument
                .split(',')
                .map(|option| json!(option.trim_matches(python_whitespace)))
                .collect();
        }
        "шкала" | "scale" | "счётчик" | "счетчик" | "count" => {
            let (minimum, rest) = bound(argument).ok_or(Invalid)?;
            let rest = rest.trim_start_matches(python_whitespace);
            let rest = rest
                .strip_prefix(['-', '–'])
                .ok_or(Invalid)?
                .trim_start_matches(python_whitespace);
            let (maximum, rest) = bound(rest).ok_or(Invalid)?;
            if !rest.is_empty() {
                return Err(Invalid);
            }
            field["minimum"] = json!(minimum);
            field["maximum"] = json!(maximum);
            if matches!(kind.as_str(), "шкала" | "scale") {
                if format!("score_{minimum}-{maximum}").len() > 32 {
                    return Err(Invalid);
                }
                field["kind"] = json!("scale");
            } else {
                field["kind"] = json!("integer");
                field["unit"] = json!("count");
                field["metric_semantics"] = json!("event_count");
            }
        }
        _ => return Err(Invalid),
    }
    Field::from_json(&field).map(|field| field.document())
}

fn bound(text: &str) -> Option<(i64, &str)> {
    let negative = text.starts_with('-');
    let digits = text.strip_prefix('-').unwrap_or(text);
    let end = digits
        .find(|character| decimal_value(character).is_none())
        .unwrap_or(digits.len());
    let digits = &digits[..end];
    if digits.is_empty() || digits.chars().count() + usize::from(negative) > 100 {
        return None;
    }
    let magnitude = digits.chars().try_fold(0i64, |value, character| {
        value
            .checked_mul(10)?
            .checked_add(i64::from(decimal_value(character)?))
    })?;
    let consumed = usize::from(negative) + end;
    Some((
        if negative { -magnitude } else { magnitude },
        &text[consumed..],
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_frozen_chat_fields() -> Result<(), Box<dyn std::error::Error>> {
        let cases: Value =
            serde_json::from_str(include_str!("../../../contracts/tracker-chat-fields.json"))?;
        for case in cases.as_array().ok_or("Expected field cases")? {
            let text = case[0].as_str().ok_or("Expected field input")?;
            let actual = parse_tracker_chat_field(text.trim_matches(python_whitespace), "field")
                .unwrap_or(Value::Null);
            assert_eq!(actual, case[1], "{text}");
        }
        Ok(())
    }
}
