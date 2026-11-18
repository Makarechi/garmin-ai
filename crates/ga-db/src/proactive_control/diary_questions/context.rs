use ga_domain::{event_input::source_calendar_wall_time, timestamp::StoredInstant};
use serde_json::json;
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
    let enabled: bool = sqlx::query_scalar(
        "SELECT count(*)=0 OR (
             coalesce(bool_or(pack_key='wellbeing' AND reminders_enabled),false)
             AND coalesce(bool_or(pack_key='general_diary' AND reminders_enabled AND tracking_enabled),false)
         ) FROM module_configs WHERE owner_id=$1",
    )
    .bind(owner)
    .fetch_one(&mut **txn)
    .await?;
    if !enabled {
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
