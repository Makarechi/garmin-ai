use std::collections::BTreeSet;

use serde_json::{Value, json};
use time::OffsetDateTime;

use super::{
    ChatFormError, answer, casefold, chat_form_prompt, message, parse_chat_form_time,
    parse_chat_form_value,
    presentation::{fields, initial_values, steps},
};
use crate::{
    event_payload::python_whitespace,
    event_schema::{entry_validator, python_equal},
    timestamp::{OffsetTimestamp, StoredInstant},
};

pub enum ChatFormProgress {
    Continue { state: Value, reply: String },
    Submit { state: Value, submission: Value },
    Cancelled(String),
}

/// Advance one answer without mutating the pinned form on a rejected answer.
pub fn advance_chat_form(
    form: &Value,
    schema: &Value,
    current: &Value,
    text: &str,
    now: OffsetDateTime,
) -> Result<ChatFormProgress, ChatFormError> {
    let locale = current["locale"].as_str().ok_or(ChatFormError::Invalid)?;
    let changed = || {
        ChatFormProgress::Cancelled(
            message(
                locale,
                "Трекер изменился. Откройте актуальное меню.",
                "Tracker changed. Open the current menu.",
            )
            .into(),
        )
    };
    if form["schema_hash"] != current["schema_hash"] {
        return Ok(changed());
    }
    let order = current["field_order"]
        .as_array()
        .ok_or(ChatFormError::Invalid)?;
    let recorded = order
        .iter()
        .map(|name| name.as_str().ok_or(ChatFormError::Invalid))
        .collect::<Result<BTreeSet<_>, _>>()?;
    let expected = fields(form)?
        .iter()
        .filter(|field| !(field["has_const"] == true && field["required"] == true))
        .map(|field| field["name"].as_str().ok_or(ChatFormError::Invalid))
        .collect::<Result<BTreeSet<_>, _>>()?;
    if order.len() != expected.len() || recorded != expected {
        return Ok(changed());
    }
    let steps = steps(form, order)?;
    let index = current["step"].as_u64().ok_or(ChatFormError::Invalid)? as usize;
    let Some(step) = steps.get(index) else {
        return Ok(ChatFormProgress::Cancelled(
            message(
                locale,
                "Форма уже заполнена. Откройте трекер заново.",
                "Form already completed. Open the tracker again.",
            )
            .into(),
        ));
    };
    let mut state = current.clone();
    if let Err(error) = apply_answer(form, schema, &mut state, text, step, now) {
        let prefix = match error {
            ChatFormError::Answer(detail) => format!("{detail}. "),
            ChatFormError::DecimalRange => return Err(ChatFormError::DecimalRange),
            ChatFormError::Invalid => message(
                locale,
                "Не удалось разобрать ответ. ",
                "Could not parse that answer. ",
            )
            .into(),
        };
        let reply = prefix + &chat_form_prompt(form, &state)?;
        return Ok(ChatFormProgress::Continue {
            state: current.clone(),
            reply,
        });
    }
    state["step"] = json!(index + 1);
    if index + 1 < steps.len() {
        let reply = chat_form_prompt(form, &state)?;
        return Ok(ChatFormProgress::Continue { state, reply });
    }
    let editing = current["action_id"]
        .as_str()
        .is_some_and(|id| id.starts_with("edit:"));
    let end = if !state["end"].is_null() {
        state["end"].clone()
    } else if editing
        && state
            .get("event_topology")
            .filter(|value| !value.is_null())
            .unwrap_or(&state["initial_topology"])
            == "point"
        && state["end_kept"] == true
    {
        state["start"].clone()
    } else {
        Value::Null
    };
    let submission = json!({"action_id":form["id"],"schema_hash":state["schema_hash"],"submission_id":state["submission_id"],
        "start":state["start"],"end":end,"timezone":state["timezone"],"values":state["values"],"units":state["units"]});
    Ok(ChatFormProgress::Submit { state, submission })
}

fn apply_answer(
    form: &Value,
    schema: &Value,
    state: &mut Value,
    text: &str,
    step: &str,
    now: OffsetDateTime,
) -> Result<(), ChatFormError> {
    let locale = state["locale"]
        .as_str()
        .ok_or(ChatFormError::Invalid)?
        .to_owned();
    let answer_text = text.trim_matches(python_whitespace);
    let editing = state["action_id"]
        .as_str()
        .is_some_and(|id| id.starts_with("edit:"));
    if matches!(step, "__start__" | "__end__") {
        let ending = step == "__end__";
        let name = if ending { "end" } else { "start" };
        let value = if editing && answer_text == "=" {
            if ending {
                state["end_kept"] = json!(true);
            }
            if state[name].is_null() {
                None
            } else {
                Some(
                    StoredInstant::legacy_input_with_offset(&state[name])
                        .ok_or(ChatFormError::Invalid)?,
                )
            }
        } else if ending && matches!(casefold(answer_text).as_str(), "нет" | "none") {
            state["end_kept"] = json!(false);
            None
        } else {
            let value = parse_chat_form_time(
                answer_text,
                state["timezone"].as_str().ok_or(ChatFormError::Invalid)?,
                now,
                &locale,
            )?;
            if ending {
                state["end_kept"] = json!(false);
            }
            Some(value)
        };
        if ending {
            if value.is_none() && form["topology"] == "bounded_interval" {
                return Err(answer(
                    &locale,
                    "Укажите время окончания",
                    "Enter an end time",
                ));
            }
            if let Some(value) = value {
                let start = StoredInstant::legacy_input_with_offset(&state["start"])
                    .ok_or(ChatFormError::Invalid)?;
                if value < start || form["topology"] == "bounded_interval" && value == start {
                    return Err(answer(
                        &locale,
                        "Окончание раньше начала",
                        "End time is before start time",
                    ));
                }
            }
        }
        state[name] = value.map_or(Value::Null, |value| {
            json!(OffsetTimestamp(value).to_string())
        });
        return Ok(());
    }
    let name = step.strip_prefix("field:").ok_or(ChatFormError::Invalid)?;
    let field = fields(form)?
        .iter()
        .find(|field| field["name"] == name)
        .ok_or(ChatFormError::Invalid)?;
    let kind = field["input"].as_str().ok_or(ChatFormError::Invalid)?;
    let value = if editing && answer_text == "=" && state["values"].get(name).is_some() {
        state["values"][name].clone()
    } else {
        let field_answer = if matches!(kind, "text" | "choice") && answer_text != "/skip" {
            text
        } else {
            answer_text
        };
        parse_chat_form_value(field, field_answer, &locale)?
    };
    if field["has_const"] == true
        && answer_text != "/skip"
        && !python_equal(&value, &field["const_value"])
    {
        return Err(answer(
            &locale,
            "Используйте фиксированное значение или /skip",
            "Use the fixed value or /skip",
        ));
    }
    if kind == "json" && answer_text != "/skip" {
        let mut candidate = state["values"].clone();
        candidate[name] = value.clone();
        let validator = entry_validator(schema).map_err(|_| ChatFormError::Invalid)?;
        let pointer = format!("/{}", name.replace('~', "~0").replace('/', "~1"));
        if validator.iter_errors(&candidate).any(|error| {
            let path = error.instance_path().to_string();
            path == pointer
                || path
                    .strip_prefix(&pointer)
                    .is_some_and(|suffix| suffix.starts_with('/'))
        }) {
            return Err(answer(
                &locale,
                "JSON не соответствует схеме поля",
                "JSON does not match the field schema",
            ));
        }
    }
    if !value.is_null() || matches!(kind, "choice" | "json") && answer_text != "/skip" {
        state["values"][name] = value;
        if field["unit"].as_str().is_some_and(|unit| !unit.is_empty()) {
            state["units"][name] = field["unit"].clone();
        }
    } else {
        state["values"]
            .as_object_mut()
            .ok_or(ChatFormError::Invalid)?
            .remove(name);
        state["units"]
            .as_object_mut()
            .ok_or(ChatFormError::Invalid)?
            .remove(name);
    }
    Ok(())
}

/// Restart only the field answers after the validated submission was refused.
pub fn restart_chat_form_fields(
    form: &Value,
    state: &mut Value,
    validation: bool,
) -> Result<String, ChatFormError> {
    let locale = state["locale"]
        .as_str()
        .ok_or(ChatFormError::Invalid)?
        .to_owned();
    let order = state["field_order"]
        .as_array()
        .ok_or(ChatFormError::Invalid)?;
    if order.is_empty() {
        return Err(answer(
            &locale,
            "Схема трекера не позволяет заполнить запись. Исправьте определение трекера.",
            "This tracker schema cannot produce a valid entry. Correct its definition.",
        ));
    }
    let count = steps(form, order)?.len();
    state["step"] = json!(count - order.len());
    let editing = state["action_id"]
        .as_str()
        .is_some_and(|id| id.starts_with("edit:"));
    let mut initial = form.clone();
    if !editing {
        initial["initial_values"] = json!({});
        initial["initial_units"] = json!({});
    }
    let (values, units) = initial_values(&initial)?;
    state["values"] = values;
    state["units"] = units;
    let detail = if validation {
        message(&locale, "Проверьте значения", "Check the values")
    } else {
        message(&locale, "Сократите значения", "Shorten the values")
    };
    Ok(format!("{detail}. {}", chat_form_prompt(form, state)?))
}
