use std::collections::BTreeSet;

use ga_domain::{event_input::source_offset_datetime, timestamp::StoredInstant};
use serde_json::{Map, Value, json};
use time::{Date, format_description::well_known::Iso8601};

use super::SLEEP_REVIEW;

/// Claim only unambiguous sleep-only questions. Mixed topics and other date
/// expressions go through the ordinary plan and can still select this reader.
pub(crate) fn sleep_review_period(question: &str, today: Date) -> Option<Map<String, Value>> {
    let lower = question.to_lowercase();
    let mut dates = Vec::new();
    let mut sleep = false;
    let mut request = question.trim_end().ends_with('?');
    let mut range = false;
    for word in lower
        .split(|c: char| !c.is_alphanumeric() && c != '-')
        .filter(|s| !s.is_empty())
    {
        let date = match word {
            "today" | "сегодня" => today,
            "yesterday" | "вчера" => today.previous_day()?,
            "сон" | "сна" | "сне" | "спал" | "спала" | "sleep" | "slept" => {
                sleep = true;
                continue;
            }
            "расскажи" | "покажи" | "сравни" | "разбери" | "как" | "what" | "how" | "tell"
            | "show" | "compare" | "review" => {
                request = true;
                continue;
            }
            "по" | "to" | "through" => {
                range = true;
                continue;
            }
            "я"
            | "мой"
            | "за"
            | "с"
            | "и"
            | "пожалуйста"
            | "подробно"
            | "did"
            | "was"
            | "i"
            | "my"
            | "me"
            | "about"
            | "and"
            | "from"
            | "on"
            | "please"
            | "the"
            | "in"
            | "detail" => continue,
            candidate => Date::parse(candidate, &Iso8601::DATE).ok()?,
        };
        dates.push(date);
    }
    dates.sort_unstable();
    dates.dedup();
    let (start, end) = match dates.as_slice() {
        [day] => (*day, *day),
        [start, end] => (*start, *end),
        _ => return None,
    };
    let days = (end - start).whole_days();
    if !(sleep && request) || start.year() < 1 || end > today || days >= 31 || (days > 1 && !range)
    {
        return None;
    }
    Some(Map::from_iter([
        ("start".into(), json!(start.to_string())),
        ("end".into(), json!(end.to_string())),
    ]))
}

pub(super) fn render_sleep_reviews(evidence: &[Value], language: &str) -> String {
    let reviews: Vec<_> = evidence
        .iter()
        .filter(|item| item["tool"] == SLEEP_REVIEW && item["result"]["rows"].is_array())
        .map(|item| &item["result"])
        .collect();
    let multiple = reviews.len() > 1;
    let mut displayed_days = BTreeSet::new();
    let mut displayed_histories = BTreeSet::new();
    reviews
        .into_iter()
        .filter_map(|result| {
            let history = &result["baseline"];
            let dates = history["start"].as_str().zip(history["end"].as_str());
            let mut lines = render_sleep_review(result, language, &mut displayed_days);
            if dates.is_none_or(|dates| displayed_histories.insert(dates)) {
                lines.extend(render_history(
                    history,
                    language,
                    dates.filter(|_| multiple),
                ));
            }
            (!lines.is_empty()).then(|| lines.join("\n"))
        })
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn render_sleep_review<'a>(
    result: &'a Value,
    language: &str,
    displayed_days: &mut BTreeSet<&'a str>,
) -> Vec<String> {
    let russian = russian(language);
    let Some(rows) = result["rows"].as_array() else {
        return Vec::new();
    };
    let timezone = result["timezone"].as_str().unwrap_or("UTC");
    let mut lines = Vec::new();
    let labels = if russian {
        [
            "Сон",
            "Оценка Garmin",
            "Глубокий",
            "Лёгкий",
            "REM",
            "Бодрствование",
            "Дневной сон",
        ]
    } else {
        [
            "Asleep",
            "Garmin score",
            "Deep",
            "Light",
            "REM",
            "Awake",
            "Naps",
        ]
    };
    for row in rows {
        let Some(date) = row["day"]
            .as_str()
            .filter(|date| displayed_days.insert(date))
        else {
            continue;
        };
        if row["available"] != true && row["reported_naps"].is_null() {
            let missing = if russian {
                "Данные пока недоступны"
            } else {
                "Data is currently unavailable"
            };
            lines.push(format!("{date} — {missing}"));
            continue;
        }
        let values = &row["values"];
        let mut summary = vec![date.to_owned()];
        let clock = |key| {
            let instant = row["main_interval"][key]
                .as_str()?
                .parse::<StoredInstant>()
                .ok()?;
            let local = source_offset_datetime(instant, timezone)?;
            Some(format!("{:02}:{:02}", local.hour(), local.minute()))
        };
        if rows.len() <= 7
            && let Some((start, end)) = clock("start").zip(clock("end"))
        {
            summary.push(format!("{start}–{end}"));
        }
        if row["available"] != true {
            summary.push(
                if russian {
                    "Основной сон пока недоступен"
                } else {
                    "Main sleep is currently unavailable"
                }
                .into(),
            );
        }
        if let Some(value) = minutes(&values["duration_minutes"], russian) {
            summary.push(format!("{} {value}", labels[0]));
        }
        if let Some(score) = values["score"].as_f64() {
            summary.push(format!("{} {score}/100", labels[1]));
        }
        lines.push(summary.join(" · "));
        if rows.len() > 7 {
            continue;
        }
        let mut details = Vec::new();
        for (key, label) in [
            ("deep_minutes", labels[2]),
            ("light_minutes", labels[3]),
            ("rem_minutes", labels[4]),
            ("awake_minutes", labels[5]),
        ] {
            if let Some(value) = minutes(&values[key], russian) {
                details.push(format!("{label} {value}"));
            }
        }
        if let Some(value) = minutes(&row["reported_naps"]["duration_minutes"], russian) {
            details.push(format!("{} {value}", labels[6]));
        } else if let Some(count) = row["reported_naps"]["count"].as_u64() {
            details.push(format!("{} {count}", labels[6]));
        }
        if !details.is_empty() {
            lines.push(details.join(" · "));
        }
        if rows.len() == 2
            && let Some(delta) =
                row["change_from_previous_requested_night"]["duration_minutes"].as_f64()
            && delta != 0.0
            && let Some(value) = minutes(&json!(delta.abs()), russian)
        {
            let label = match (russian, delta.is_sign_positive()) {
                (true, true) => "Больше сна, чем в предыдущую ночь",
                (true, false) => "Меньше сна, чем в предыдущую ночь",
                (false, true) => "More sleep than the previous night",
                (false, false) => "Less sleep than the previous night",
            };
            lines.push(format!("{label} — {value}"));
        }
    }
    lines
}

fn render_history(history: &Value, language: &str, dates: Option<(&str, &str)>) -> Vec<String> {
    let russian = russian(language);
    let mut lines = Vec::new();
    let baseline = &history["metrics"]["duration_minutes"];
    if let Some(count) = baseline["nights"].as_u64()
        && count > 0
    {
        let label = if russian {
            "Ночей с данными о длительности за предыдущие 28 дней"
        } else {
            "Nights with duration data over the preceding 28 days"
        };
        if let Some((start, end)) = dates {
            lines.push(format!("{label} ({start}–{end}) — {count}"));
        } else {
            lines.push(format!("{label} — {count}"));
        }
        if let Some(median) = minutes(&baseline["median"], russian) {
            let label = if russian {
                "Медианная длительность сна"
            } else {
                "Median sleep duration"
            };
            lines.push(format!("{label} — {median}"));
        }
    }
    lines
}

fn russian(language: &str) -> bool {
    language
        .split('-')
        .next()
        .is_some_and(|code| code.eq_ignore_ascii_case("ru"))
}

fn minutes(input: &Value, russian: bool) -> Option<String> {
    let value = input.as_f64()?;
    if !(0.0..=1440.0).contains(&value) {
        return None;
    }
    let hours = (value / 60.0).floor();
    let mins = value % 60.0;
    Some(match (russian, hours > 0.0) {
        (true, true) => format!("{hours:.0} ч {mins:.0} мин"),
        (false, true) => format!("{hours:.0} h {mins:.0} min"),
        (true, false) => format!("{mins:.0} мин"),
        (false, false) => format!("{mins:.0} min"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selects_relative_and_explicit_sleep_dates() -> Result<(), time::error::Parse> {
        let today = Date::parse("2026-11-27", &Iso8601::DATE)?;
        let range = json!({"start":"2026-11-26","end":"2026-11-27"});
        assert_eq!(
            sleep_review_period("Расскажи, как я спал вчера и сегодня", today).map(Value::Object),
            Some(range.clone())
        );
        assert_eq!(
            sleep_review_period("Review my sleep from 2026-11-26 to 2026-11-27", today)
                .map(Value::Object),
            Some(range)
        );
        assert!(sleep_review_period("как кофе повлиял на сон вчера?", today).is_none());
        assert!(sleep_review_period("во сколько я спал вчера?", today).is_none());
        assert!(sleep_review_period("сон за 2026-11-30", today).is_none());
        Ok(())
    }
}
