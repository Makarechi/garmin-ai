use ga_domain::{event_input::source_calendar_wall_time, timestamp::StoredInstant};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

use super::{ProactiveControlError, Question};
use crate::diary::question_context::{coverage, display, elevated_windows, measurements};

pub(super) async fn generate(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    now: OffsetDateTime,
    timezone: &str,
) -> Result<u64, ProactiveControlError> {
    if !enabled(txn, owner).await? {
        return Ok(0);
    }

    let windows =
        elevated_windows(txn, now - Duration::hours(3), now - Duration::minutes(15)).await?;
    let mut generated = 0;
    for (left, right) in windows {
        let Some((threshold, samples, ratio)) =
            measurements(txn, now, left, right, timezone).await?
        else {
            continue;
        };
        let (coverage, explained) = coverage(txn, left, right).await?;
        if explained || already_asked(txn, now, left, right).await? {
            continue;
        }
        let start = display(left);
        generated += Question {
            kind: "context",
            text: question_text(left, right, timezone)?,
            evidence: json!({
                "start":start,"end":display(right),"baseline_hr_p95":threshold,
                "hr_samples":samples,"hr_coverage_ratio":ratio,"timezone":timezone,
                "status":"inferred","context_coverage":coverage,
            }),
            priority: 0.7,
            event: None,
            key: format!("context:{start}"),
        }
        .insert(txn, now)
        .await?;
    }
    Ok(generated)
}

pub(in crate::proactive_control) async fn enabled(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT count(*)=0 OR (
             coalesce(bool_or(pack_key='wellbeing' AND reminders_enabled),false)
             AND coalesce(bool_or(pack_key='general_diary' AND reminders_enabled AND tracking_enabled),false)
         ) FROM module_configs WHERE owner_id=$1",
    )
    .bind(owner)
    .fetch_one(&mut **txn)
    .await
}

pub(in crate::proactive_control) async fn refresh(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    stored: &str,
    now: OffsetDateTime,
    fallback_timezone: &str,
) -> Result<Option<String>, ProactiveControlError> {
    let invalid = || ProactiveControlError::InvalidStoredState;
    let mut evidence: Map<String, Value> = serde_json::from_str(stored).map_err(|_| invalid())?;
    let (Some(left), Some(right)) = (
        evidence
            .get("start")
            .and_then(StoredInstant::from_legacy_input),
        evidence
            .get("end")
            .and_then(StoredInstant::from_legacy_input),
    ) else {
        return Ok(None);
    };
    if right <= left {
        return Ok(None);
    }
    let timezone = evidence
        .get("timezone")
        .and_then(Value::as_str)
        .unwrap_or(fallback_timezone);
    let observed = measurements(txn, now, left.into(), right.into(), timezone).await?;
    let (coverage, explained) = coverage(txn, left.into(), right.into()).await?;
    let text = if let Some((threshold, samples, ratio)) = observed
        && !explained
    {
        let text = current_question_text(&coverage, timezone)?;
        evidence.insert("baseline_hr_p95".into(), json!(threshold));
        evidence.insert("hr_samples".into(), json!(samples));
        evidence.insert("hr_coverage_ratio".into(), json!(ratio));
        Some(text)
    } else {
        None
    };
    evidence.insert("context_coverage".into(), coverage);
    sqlx::query(
        "UPDATE pending_questions SET evidence=$2::jsonb, text=coalesce($3,text) WHERE id=$1",
    )
    .bind(id)
    .bind(Value::Object(evidence).to_string())
    .bind(text.as_deref())
    .execute(&mut **txn)
    .await?;
    Ok(text)
}

fn current_question_text(
    coverage: &Value,
    timezone: &str,
) -> Result<String, ProactiveControlError> {
    let invalid = || ProactiveControlError::InvalidStoredState;
    let gaps = coverage["uncovered_intervals"]
        .as_array()
        .ok_or_else(invalid)?;
    let minutes = coverage["uncovered_seconds"].as_f64().ok_or_else(invalid)? / 60.0;
    let local = |instant| {
        let instant = StoredInstant::from_legacy_input(instant).ok_or_else(invalid)?;
        let (day, at) = source_calendar_wall_time(instant, timezone).ok_or_else(invalid)?;
        Ok::<_, ProactiveControlError>(format!(
            "{:02}.{:02} {:02}:{:02}",
            day.day(),
            u8::from(day.month()),
            at.hour(),
            at.minute()
        ))
    };
    let windows = gaps
        .iter()
        .take(3)
        .map(|gap| Ok(format!("{}–{}", local(&gap["start"])?, local(&gap["end"])?)))
        .collect::<Result<Vec<_>, ProactiveControlError>>()?
        .join("; ");
    let additional = if gaps.len() > 3 {
        "; есть другие промежутки"
    } else {
        ""
    };
    let minutes = question_minutes(minutes)?;
    Ok(format!(
        "Часы записали повышенные показатели стресса и пульса. Неизвестный контекст: {minutes} мин; {windows}{additional}. Помните, чем занимались? Можно ответить «не помню»."
    ))
}

fn question_minutes(minutes: f64) -> Result<String, ProactiveControlError> {
    // Match the six significant digits of the existing question text.
    let scientific = format!("{minutes:.5e}");
    let invalid = || ProactiveControlError::InvalidStoredState;
    let (mantissa, exponent) = scientific.split_once('e').ok_or_else(invalid)?;
    let exponent: i32 = exponent.parse().map_err(|_| invalid())?;
    let trimmed = |digits: &str| {
        digits
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_string()
    };
    if !(-4..6).contains(&exponent) {
        return Ok(format!("{}e{exponent:+03}", trimmed(mantissa)));
    }
    let decimals = usize::try_from(5 - exponent).map_err(|_| invalid())?;
    let decimal = format!("{minutes:.decimals$}");
    Ok(if decimals == 0 {
        decimal
    } else {
        trimmed(&decimal)
    })
}

async fn already_asked(
    txn: &mut Transaction<'_, Postgres>,
    now: OffsetDateTime,
    left: OffsetDateTime,
    right: OffsetDateTime,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM pending_questions
         WHERE kind='context' AND expires_at>$1 AND (status<>'cancelled' OR sent_at IS NOT NULL)
           AND NULLIF(evidence->>'start','')::timestamptz<$3
           AND NULLIF(evidence->>'end','')::timestamptz>$2)",
    )
    .bind(now)
    .bind(left)
    .bind(right)
    .fetch_one(&mut **txn)
    .await
}

fn question_text(
    left: OffsetDateTime,
    right: OffsetDateTime,
    timezone: &str,
) -> Result<String, ProactiveControlError> {
    let invalid = || ProactiveControlError::InvalidStoredState;
    let local = |at| {
        let at = StoredInstant::try_from(at).map_err(|_| invalid())?;
        source_calendar_wall_time(at, timezone).ok_or_else(invalid)
    };
    let (day, start) = local(left)?;
    let (end_day, end) = local(right)?;
    let calendar_date = |day: time::Date| {
        format!(
            "{:02}.{:02}.{:04}",
            day.day(),
            u8::from(day.month()),
            day.year()
        )
    };
    let ending = format!("{:02}:{:02}", end.hour(), end.minute());
    let ending = if day == end_day {
        ending
    } else {
        format!("{} {ending}", calendar_date(end_day))
    };
    Ok(format!(
        "{} с {:02}:{:02} до {ending} часы записали повышенные показатели стресса и пульса. Контекст этого интервала известен не полностью. Помните, чем занимались? Можно ответить «не помню».",
        calendar_date(day),
        start.hour(),
        start.minute()
    ))
}
