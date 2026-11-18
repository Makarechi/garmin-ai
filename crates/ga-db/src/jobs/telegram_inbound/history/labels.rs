use ga_domain::timestamp::StoredInstant;
use serde_json::Value;

use crate::diary::{DiaryReadError, caffeine_total};

pub(super) fn label(
    kind: &str,
    payload: &Value,
    end: Option<StoredInstant>,
    now: StoredInstant,
) -> Result<String, DiaryReadError> {
    let result = match kind {
        "activity_effort" => format!(
            "Тяжесть тренировки: {}/10, активность {}",
            payload["perceived_exertion"],
            required_text(payload, "activity_id")?,
        ),
        "wellbeing_observation" => {
            let mut details = Vec::new();
            for (field, name) in [
                ("energy", "энергия"),
                ("restedness", "отдохнувший"),
                ("pain", "боль"),
                ("functional_impact", "помехи обычным делам"),
            ] {
                if let Some(rating) = payload[field].as_i64() {
                    details.push(format!("{name}: {rating}/10"));
                }
            }
            append_text(&mut details, &payload["notes"]);
            format!("Самочувствие: {}", details.join("; "))
        }
        "caffeine_log_complete" => {
            format!(
                "Полнота дневника кофеина: {}",
                required_text(payload, "description")?
            )
        }
        "headache_observation" => {
            let answer = |field| match payload.get(field).and_then(Value::as_str) {
                Some("yes") => Ok("да"),
                Some("no") => Ok("нет"),
                Some("unknown") => Ok("неизвестно"),
                _ => Err(DiaryReadError::InvalidEvent),
            };
            format!(
                "Головная боль: {}; мигрень: {}",
                answer("headache")?,
                answer("migraine")?
            )
        }
        "caffeine" => caffeine(payload)?,
        "symptom_observation" => {
            let severity = payload["severity"]
                .as_i64()
                .map_or_else(|| "не указана".to_owned(), |rating| format!("{rating}/10"));
            let mut details = vec![format!("Наблюдение симптомов: боль {severity}")];
            if let Some(aura) = payload["aura"].as_bool() {
                details.push(
                    if aura {
                        "аура: да"
                    } else {
                        "аура: нет"
                    }
                    .into(),
                );
            }
            if let Some(symptoms) = payload["symptoms"]
                .as_array()
                .filter(|items| !items.is_empty())
            {
                let symptoms = symptoms
                    .iter()
                    .map(|item| item.as_str().ok_or(DiaryReadError::InvalidEvent))
                    .collect::<Result<Vec<_>, _>>()?;
                details.push(symptoms.join(", "));
            }
            append_text(&mut details, &payload["impact"]);
            details.join("; ")
        }
        "migraine" => {
            let severity = payload["severity"]
                .as_i64()
                .map_or_else(String::new, |rating| format!(", {rating}/10"));
            let status = if end.is_some_and(|end| end <= now) {
                "завершена"
            } else {
                "ещё не завершена"
            };
            format!("Мигрень{severity}, {status}")
        }
        "medication" => {
            let name = nonempty_text(&payload["name"]).unwrap_or("название неизвестно");
            let dose = payload
                .get("dose")
                .filter(|dose| !dose.is_null())
                .map_or_else(|| "доза неизвестна".to_owned(), Value::to_string);
            let unit = nonempty_text(&payload["unit"]).unwrap_or("единица неизвестна");
            format!("Лекарство: {name}, {dose} {unit}")
        }
        _ => payload["description"].as_str().unwrap_or(kind).to_owned(),
    };
    Ok(result)
}

fn append_text(details: &mut Vec<String>, value: &Value) {
    if let Some(text) = nonempty_text(value) {
        details.push(text.to_owned());
    }
}

fn nonempty_text(value: &Value) -> Option<&str> {
    value.as_str().filter(|text| !text.is_empty())
}

fn required_text<'a>(payload: &'a Value, field: &str) -> Result<&'a str, DiaryReadError> {
    payload[field].as_str().ok_or(DiaryReadError::InvalidEvent)
}

fn caffeine(payload: &Value) -> Result<String, DiaryReadError> {
    let total = caffeine_total(payload)?;
    let servings = payload
        .get("servings")
        .map_or_else(|| "1".to_owned(), Value::to_string);
    let label = format!(
        "Кофе: {}, порций: {servings}",
        required_text(payload, "beverage")?
    );
    let provenance = total["provenance"].as_str();
    let numbers = ["min", "estimate", "max"].map(|key| total[key].as_f64().map(dose_display));
    let dose = match numbers {
        [Some(min), _, Some(max)] => format!("{min}–{max}"),
        [_, Some(estimate), _] if provenance == Some("reported_label") => estimate,
        [_, Some(estimate), _] => format!("около {estimate}"),
        [Some(min), _, _] => format!("не менее {min}"),
        [_, _, Some(max)] => format!("не более {max}"),
        _ => return Ok(format!("{label}; суммарная доза неизвестна")),
    };
    let source = match provenance {
        Some("reported_label") => "по этикетке",
        Some("estimated") => "оценка",
        _ => "источник дозы не указан",
    };
    Ok(format!("{label}; всего кофеина {dose} мг ({source})"))
}

fn dose_display(value: f64) -> String {
    // The deployed diary uses the general format with six significant digits.
    let scientific = format!("{value:.5e}");
    let Some((mantissa, exponent)) = scientific.split_once('e') else {
        return scientific;
    };
    let Ok(exponent) = exponent.parse::<i32>() else {
        return scientific;
    };
    if !(-4..6).contains(&exponent) {
        let digits = mantissa.trim_end_matches('0').trim_end_matches('.');
        return format!("{digits}e{exponent:+03}");
    }
    let decimals = (5 - exponent) as usize;
    let decimal = format!("{value:.decimals$}");
    if decimals == 0 {
        decimal
    } else {
        decimal
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_owned()
    }
}
