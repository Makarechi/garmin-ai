use std::fmt::Write;

use num_bigint::BigInt;
use serde_json::{Value, json};

use super::{
    ChatFormError, answer, message,
    values::{chat_choice_labels, json_text, python_string, storable},
};
use crate::{event_schema::custom::ExactNumber, timestamp::StoredInstant};

pub struct ChatFormStart {
    pub state: Value,
    pub reply: String,
}

pub(super) fn fields(form: &Value) -> Result<&[Value], ChatFormError> {
    form["fields"]
        .as_array()
        .map(Vec::as_slice)
        .ok_or(ChatFormError::Invalid)
}

pub(super) fn steps(form: &Value, order: &[Value]) -> Result<Vec<String>, ChatFormError> {
    let mut steps = vec!["__start__".to_owned()];
    if form["topology"] != "point" {
        steps.push("__end__".to_owned());
    }
    for name in order {
        steps.push(format!(
            "field:{}",
            name.as_str().ok_or(ChatFormError::Invalid)?
        ));
    }
    Ok(steps)
}

pub(super) fn initial_values(form: &Value) -> Result<(Value, Value), ChatFormError> {
    let mut values = form["initial_values"]
        .as_object()
        .cloned()
        .ok_or(ChatFormError::Invalid)?;
    let mut units = form["initial_units"]
        .as_object()
        .cloned()
        .ok_or(ChatFormError::Invalid)?;
    for field in fields(form)? {
        let name = field["name"].as_str().ok_or(ChatFormError::Invalid)?;
        if field["has_const"] == true && (field["required"] == true || values.contains_key(name)) {
            values.insert(name.to_owned(), field["const_value"].clone());
            if field["unit"].as_str().is_some_and(|unit| !unit.is_empty()) {
                units.insert(name.to_owned(), field["unit"].clone());
            }
        }
    }
    Ok((Value::Object(values), Value::Object(units)))
}

/// Pin the form revision and submission identity before asking its first question.
pub fn begin_chat_form(
    form: &Value,
    timezone: &str,
    locale: &str,
) -> Result<ChatFormStart, ChatFormError> {
    let editing = form["action"]["kind"] == "edit_entry";
    if !editing && (form["action"]["kind"] != "create_entry" || form["submission_id"].is_null()) {
        return Err(ChatFormError::Invalid);
    }
    let rows = fields(form)?;
    for field in rows {
        if field["required"] == true
            && field["has_const"] == true
            && !storable(field, &field["const_value"])?
        {
            return Err(answer(
                locale,
                "Фиксированное значение не соответствует схеме трекера. Откройте трекер в приложении.",
                "A fixed value does not match the tracker schema. Open the tracker in the app.",
            ));
        }
    }
    if minimum_entry_length(form)? > 65536 {
        return Err(answer(
            locale,
            "Минимальная запись превышает 64 КиБ. Заполните трекер в приложении.",
            "The minimum entry exceeds 64 KiB. Fill the tracker in the app.",
        ));
    }
    for field in rows {
        if !matches!(field["input"].as_str(), Some("integer" | "number"))
            || field["required"] != true
            || editing
                && form["initial_values"]
                    .get(field["name"].as_str().ok_or(ChatFormError::Invalid)?)
                    .is_some()
        {
            continue;
        }
        let lower = ExactNumber::read(&field["minimum"]);
        let upper = ExactNumber::read(&field["maximum"]);
        if let (Some(lower), Some(upper)) = (lower, upper) {
            let open_lower = field["exclusive_minimum"] == true;
            let open_upper = field["exclusive_maximum"] == true;
            let first = if open_lower {
                lower.floor() + 1
            } else {
                lower.ceil()
            };
            let last = if open_upper {
                upper.ceil() - 1
            } else {
                upper.floor()
            };
            if lower > upper
                || lower == upper && (open_lower || open_upper)
                || field["input"] == "integer" && first > last
            {
                return Err(answer(
                    locale,
                    "У числового поля нет допустимого значения. Откройте трекер в приложении.",
                    "A numeric field has no valid value. Open the tracker in the app.",
                ));
            }
        }
    }
    if form["conditional_requirements"] == true {
        return Err(answer(
            locale,
            "У этого трекера условные обязательные поля. Заполните его в приложении.",
            "This tracker has conditional required fields. Fill it in the app.",
        ));
    }
    for field in rows {
        if field["required"] == true && unsupported_number_range(field) {
            return Err(answer(
                locale,
                "Числовое поле нельзя заполнить в чате. Откройте трекер в приложении.",
                "A numeric field cannot be filled in chat. Open the tracker in the app.",
            ));
        }
    }
    let mut too_long = form["complex_schema"] == true;
    for field in rows {
        let name = field["name"].as_str().ok_or(ChatFormError::Invalid)?;
        if field["has_const"] == true
            || field["required"] != true
            || editing && form["initial_values"].get(name).is_some()
        {
            continue;
        }
        let text_impossible = field["input"] == "text"
            && (field["min_length"].as_u64().is_some_and(|min| min > 4096)
                || field["min_length"]
                    .as_u64()
                    .zip(field["max_length"].as_u64())
                    .is_some_and(|(min, max)| min > max));
        too_long |= field["complex_json"] == true
            || text_impossible
            || field["input"] == "choice" && !sendable_choice(field)?
            || field["input"] == "json"
                && field["min_json_length"]
                    .as_u64()
                    .is_some_and(|min| min > 4096);
    }
    if too_long {
        return Err(answer(
            locale,
            "Поле требует ответ длиннее лимита Telegram. Заполните трекер в приложении.",
            "A field requires an answer longer than Telegram allows. Use the app to fill this tracker.",
        ));
    }
    let order = rows
        .iter()
        .filter(|field| !(field["has_const"] == true && field["required"] == true))
        .map(|field| field["name"].clone())
        .collect::<Vec<_>>();
    let (values, units) = initial_values(form)?;
    let state = json!({"action_id":form["id"],"schema_hash":form["schema_hash"],"submission_id":form["submission_id"],
        "timezone":form["initial_timezone"].as_str().unwrap_or(timezone),"locale":locale,"field_order":order,
        "step":0,"start":encoded_time(&form["initial_start"] )?,"end":encoded_time(&form["initial_end"])?,"event_topology":form["initial_topology"],
        "end_kept":false,"values":values,"units":units});
    let reply = chat_form_prompt(form, &state)?;
    Ok(ChatFormStart { state, reply })
}

fn stored_length(value: &Value) -> Result<usize, ChatFormError> {
    crate::event_contract::python_ascii_json(value)
        .map(|s| s.len())
        .map_err(|_| ChatFormError::Invalid)
}

fn minimum_entry_length(form: &Value) -> Result<usize, ChatFormError> {
    let required = fields(form)?
        .iter()
        .filter(|field| field["required"] == true)
        .collect::<Vec<_>>();
    let mut total = 2 + required.len().saturating_sub(1);
    for field in required {
        total = total
            .saturating_add(stored_length(&field["name"])?)
            .saturating_add(1);
        let length = if field["has_const"] == true {
            stored_length(&field["const_value"])?
        } else {
            match field["input"].as_str() {
                Some("text") => usize::try_from(field["min_length"].as_u64().unwrap_or(0))
                    .unwrap_or(usize::MAX)
                    .saturating_add(2),
                Some("boolean") => 4,
                Some("json") => usize::try_from(
                    field["min_json_storage_length"]
                        .as_u64()
                        .unwrap_or(1)
                        .max(1),
                )
                .unwrap_or(usize::MAX),
                Some("choice") => {
                    let options = field["options"].as_array().ok_or(ChatFormError::Invalid)?;
                    let labels = chat_choice_labels(options)?;
                    let mut shortest = None;
                    for (option, label) in options.iter().zip(labels) {
                        if label.encode_utf16().count() <= 4096 && storable(field, option)? {
                            let length = stored_length(option)?;
                            shortest =
                                Some(shortest.map_or(length, |prior: usize| prior.min(length)));
                        }
                    }
                    shortest.unwrap_or(1)
                }
                _ => 1,
            }
        };
        total = total.saturating_add(length);
    }
    Ok(total)
}

fn sendable_choice(field: &Value) -> Result<bool, ChatFormError> {
    let options = field["options"].as_array().ok_or(ChatFormError::Invalid)?;
    for (option, label) in options.iter().zip(chat_choice_labels(options)?) {
        if label.encode_utf16().count() <= 4096 && storable(field, option)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn unsupported_number_range(field: &Value) -> bool {
    if field["input"] != "number" {
        return false;
    }
    let (Some(lower), Some(upper)) = (
        ExactNumber::read(&field["minimum"]),
        ExactNumber::read(&field["maximum"]),
    ) else {
        return false;
    };
    let first = if field["exclusive_minimum"] == true {
        lower.floor() + BigInt::from(1)
    } else {
        lower.ceil()
    };
    let last = if field["exclusive_maximum"] == true {
        upper.ceil() - BigInt::from(1)
    } else {
        upper.floor()
    };
    if first <= last {
        return false;
    }
    let (Some(lower), Some(upper)) = (
        field["minimum"].as_f64().filter(|n| n.is_finite()),
        field["maximum"].as_f64().filter(|n| n.is_finite()),
    ) else {
        return false;
    };
    let first = if field["exclusive_minimum"] == true {
        lower.next_up()
    } else {
        lower
    };
    if field["exclusive_maximum"] == true {
        first >= upper
    } else {
        first > upper
    }
}

pub(super) fn literal(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for character in text.chars() {
        if "\\`*_{}[]()#+.!<>|~-".contains(character) {
            escaped.push('\\');
        }
        escaped.push(character);
    }
    escaped
}

fn display_time(value: &Value, timezone: &str) -> Result<String, ChatFormError> {
    if value.is_null() {
        return Ok("—".into());
    }
    let at = StoredInstant::from_legacy_input(value).ok_or(ChatFormError::Invalid)?;
    let zone = crate::timestamp::python_timezone(timezone).ok_or(ChatFormError::Invalid)?;
    let utc_offset = super::timezone_offset(&zone, at)?;
    let local = time::OffsetDateTime::from(at)
        .checked_to_offset(utc_offset)
        .ok_or(ChatFormError::Invalid)?;
    let offset = utc_offset.whole_seconds();
    let sign = if offset < 0 { '-' } else { '+' };
    let magnitude = offset.unsigned_abs();
    let mut result = format!(
        "{:04}-{:02}-{:02} {:02}:{:02}{sign}{:02}:{:02}",
        local.year(),
        u8::from(local.month()),
        local.day(),
        local.hour(),
        local.minute(),
        magnitude / 3600,
        magnitude % 3600 / 60
    );
    if magnitude % 60 != 0 {
        let _ = write!(result, ":{:02}", magnitude % 60);
    }
    Ok(result)
}

/// Render the current question from the pinned field order and historical values.
pub fn chat_form_prompt(form: &Value, state: &Value) -> Result<String, ChatFormError> {
    let locale = state["locale"].as_str().ok_or(ChatFormError::Invalid)?;
    let order = state["field_order"]
        .as_array()
        .ok_or(ChatFormError::Invalid)?;
    let steps = steps(form, order)?;
    let index = state["step"]
        .as_u64()
        .and_then(|step| usize::try_from(step).ok())
        .ok_or(ChatFormError::Invalid)?;
    let step = steps.get(index).ok_or(ChatFormError::Invalid)?;
    let editing = state["action_id"]
        .as_str()
        .is_some_and(|id| id.starts_with("edit:"));
    let keep = message(
        locale,
        " Ответьте «=», чтобы оставить прежнее значение.",
        " Reply '=' to keep the current value.",
    );
    if matches!(step.as_str(), "__start__" | "__end__") {
        let mut prompt = if step == "__start__" {
            message(
                locale,
                "Когда началась запись? Ответьте «сейчас» или укажите YYYY-MM-DD HH:MM.",
                "When did the entry start? Reply 'now' or enter YYYY-MM-DD HH:MM.",
            )
            .to_owned()
        } else {
            let ending = match form["topology"].as_str() {
                Some("bounded_interval") => "",
                Some("flexible") => message(
                    locale,
                    " или «нет», чтобы сохранить точечную запись",
                    " or 'none' to save a point entry",
                ),
                _ => message(
                    locale,
                    " или «нет», если эпизод ещё идёт",
                    " or 'none' if it is still open",
                ),
            };
            format!(
                "{}{ending}.",
                message(
                    locale,
                    "Когда запись закончилась? Укажите YYYY-MM-DD HH:MM",
                    "When did the entry end? Enter YYYY-MM-DD HH:MM"
                )
            )
        };
        if editing {
            let value = if step == "__start__" {
                &state["start"]
            } else {
                &state["end"]
            };
            let _ = write!(
                prompt,
                " {}.{keep}",
                display_time(
                    value,
                    state["timezone"].as_str().ok_or(ChatFormError::Invalid)?
                )?
            );
        }
        return Ok(prompt);
    }
    let name = step.strip_prefix("field:").ok_or(ChatFormError::Invalid)?;
    let field = fields(form)?
        .iter()
        .find(|field| field["name"] == name)
        .ok_or(ChatFormError::Invalid)?;
    let kind = field["input"].as_str().ok_or(ChatFormError::Invalid)?;
    let mut detail = String::new();
    if let Some(unit) = field["unit"].as_str().filter(|unit| !unit.is_empty()) {
        let _ = write!(detail, " ({})", literal(unit));
    }
    if field["has_const"] == true {
        let value = if kind == "json" {
            json_text(&field["const_value"], false)?
        } else {
            python_string(&field["const_value"])?
        };
        let _ = write!(
            detail,
            " ({}: {})",
            message(locale, "фиксированное значение", "fixed value"),
            literal(&value)
        );
    }
    if kind == "choice" {
        let labels =
            chat_choice_labels(field["options"].as_array().ok_or(ChatFormError::Invalid)?)?;
        let _ = write!(
            detail,
            ": {}",
            labels
                .iter()
                .map(|s| literal(s))
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    let mut bounds = Vec::new();
    for (bound, exclusive, open, closed) in [
        ("minimum", "exclusive_minimum", "> ", "≥ "),
        ("maximum", "exclusive_maximum", "< ", "≤ "),
    ] {
        if !field[bound].is_null() {
            bounds.push(format!(
                "{}{}",
                if field[exclusive] == true {
                    open
                } else {
                    closed
                },
                python_string(&field[bound])?
            ));
        }
    }
    if !bounds.is_empty() {
        let _ = write!(detail, " ({})", bounds.join(", "));
    }
    if kind == "text" {
        if let Some(min) = field["min_length"].as_u64().filter(|&min| min > 1) {
            if super::russian(locale) {
                let _ = write!(detail, " (от {min} символов)");
            } else {
                let _ = write!(detail, " ({min}+ characters)");
            }
        }
        detail.push_str(message(
            locale,
            " (пустое: =/empty; буквальное /empty: ==/empty; другие команды — с =)",
            " (empty: =/empty; literal /empty: ==/empty; prefix other commands with =)",
        ));
        if let Some(max) = field["max_length"].as_u64() {
            if super::russian(locale) {
                let _ = write!(detail, " (до {max} символов)");
            } else {
                let _ = write!(detail, " (up to {max} characters)");
            }
        }
    }
    if kind == "json" {
        detail.push_str(message(
            locale,
            " (отправьте JSON, например [\"a\", \"b\"] или {\"key\": \"value\"})",
            " (send JSON, for example [\"a\", \"b\"] or {\"key\": \"value\"})",
        ));
    }
    let optional = if field["required"] == true {
        ""
    } else {
        message(
            locale,
            " Ответьте /skip, чтобы пропустить; =/skip сохранит буквальное значение.",
            " Reply /skip to skip; =/skip saves the literal value.",
        )
    };
    let mut prompt = format!(
        "{}{detail}?{optional}",
        literal(field["label"].as_str().ok_or(ChatFormError::Invalid)?)
    );
    let equals = message(
        locale,
        " Для значения «=» ответьте «==».",
        " Reply '==' to enter a literal '='.",
    );
    let current = if editing {
        &state["values"][name]
    } else {
        &Value::Null
    };
    if !current.is_null() {
        let displayed = if kind == "json" {
            json_text(current, false)?
        } else {
            python_string(current)?
        };
        let _ = write!(prompt, " {}.{keep}", literal(&displayed));
        if matches!(kind, "text" | "choice") {
            prompt.push_str(equals);
        }
    } else if matches!(kind, "text" | "choice") {
        prompt.push_str(equals);
    }
    Ok(prompt)
}

fn encoded_time(value: &Value) -> Result<Value, ChatFormError> {
    if value.is_null() {
        return Ok(Value::Null);
    }
    let at = StoredInstant::legacy_input_with_offset(value).ok_or(ChatFormError::Invalid)?;
    Ok(json!(crate::timestamp::OffsetTimestamp(at).to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_an_oversized_required_field_before_prompting() {
        let form = json!({
            "action": {"kind": "create_entry"},
            "submission_id": "synthetic",
            "fields": [
                {"name": "note", "input": "text", "required": true, "min_length": u64::MAX},
                {"name": "follow_up", "input": "boolean", "required": true}
            ]
        });

        assert!(matches!(
            begin_chat_form(&form, "UTC", "en"),
            Err(ChatFormError::Answer(message)) if message.contains("exceeds 64 KiB")
        ));
    }
}
