use std::fmt::Write as _;

use futures_util::TryStreamExt;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{DiaryWriteError, Question, instant, valid_events};

const KINDS: &[&str] = &[
    "context",
    "nap",
    "meal",
    "stressor",
    "travel",
    "mood",
    "note",
    "illness",
    "alcohol",
    "hydration",
];

pub(super) enum Outcome {
    Answer(Option<Uuid>),
    Finished,
}

pub(super) async fn answer(
    txn: &mut Transaction<'_, Postgres>,
    question: &mut Question,
    now: OffsetDateTime,
    configured_timezone: &str,
) -> Result<Outcome, DiaryWriteError> {
    let left = instant(&question.evidence["start"])?;
    let right = instant(
        question
            .evidence
            .get("end")
            .ok_or(DiaryWriteError::InvalidRecordedState)?,
    )?;
    let (coverage, explained) = coverage(txn, left, right).await?;
    question
        .evidence
        .insert("context_coverage".into(), coverage);
    if matches!(question.status.as_str(), "acknowledged" | "cancelled")
        && question.evidence.get("answer_kind").and_then(Value::as_str) == Some("unknown")
    {
        question.status = if explained {
            "cancelled"
        } else {
            "acknowledged"
        }
        .into();
        return Ok(Outcome::Finished);
    }
    if let Some(events) = question.evidence.get("reply_events")
        && valid_events(txn, events).await? == Some(true)
    {
        question.status = "answered".into();
        return Ok(Outcome::Finished);
    }
    let covering = sqlx::query_scalar(
        "SELECT id FROM events WHERE NOT deleted AND status='confirmed' AND kind=ANY($3)
          AND start<=$1 AND \"end\">=$2 ORDER BY start LIMIT 1",
    )
    .bind(left)
    .bind(right)
    .bind(KINDS)
    .fetch_optional(&mut **txn)
    .await?;
    if covering.is_none() {
        if explained {
            question.status = "cancelled".into();
            return Ok(Outcome::Finished);
        }
        if matches!(question.status.as_str(), "cancelled" | "answered") {
            let timezone = question
                .evidence
                .get("timezone")
                .and_then(Value::as_str)
                .unwrap_or(configured_timezone);
            let Some((threshold, samples, coverage)) =
                measurements(txn, now, left, right, timezone).await?
            else {
                question.status = "cancelled".into();
                return Ok(Outcome::Finished);
            };
            question.reactivate(now);
            question.evidence.extend([
                ("baseline_hr_p95".into(), json!(threshold)),
                ("hr_samples".into(), json!(samples)),
                ("hr_coverage_ratio".into(), json!(coverage)),
            ]);
        }
    }
    Ok(Outcome::Answer(covering))
}

pub(crate) async fn coverage(
    txn: &mut Transaction<'_, Postgres>,
    left: OffsetDateTime,
    right: OffsetDateTime,
) -> Result<(Value, bool), DiaryWriteError> {
    if right <= left {
        return Err(DiaryWriteError::InvalidRecordedState);
    }
    let mut intervals = sqlx::query_as::<_, (OffsetDateTime, OffsetDateTime)>(
        "SELECT * FROM (
             SELECT start,\"end\" FROM activities WHERE start<$2 AND \"end\">$1
             UNION ALL
             SELECT start,\"end\" FROM timeline_intervals
              WHERE start<$2 AND \"end\">$1 AND confirmed AND source NOT IN ('calendar','external_calendar')
             UNION ALL
             SELECT start,\"end\" FROM events
              WHERE start<$2 AND \"end\">$1 AND NOT deleted AND status='confirmed'
                AND source<>'inferred' AND kind=ANY($3)
         ) AS intervals ORDER BY start,\"end\"",
    ).bind(left).bind(right).bind(KINDS).fetch(&mut **txn);
    let mut remaining = 0.0;
    let mut cursor = left;
    let mut uncovered = Vec::new();
    while let Some((start, end)) = intervals.try_next().await? {
        let start = if start > left { start } else { left };
        let end = if end < right { end } else { right };
        if end <= start {
            continue;
        }
        if start > cursor {
            uncovered.push(json!({"start":display(cursor),"end":display(start)}));
            remaining += (start - cursor).as_seconds_f64();
        }
        if end > cursor {
            cursor = end;
        }
    }
    if cursor < right {
        uncovered.push(json!({"start":display(cursor),"end":display(right)}));
        remaining += (right - cursor).as_seconds_f64();
    }
    let duration = (right - left).as_seconds_f64();
    Ok((
        json!({
            "covered_seconds": duration-remaining,
            "uncovered_seconds": remaining,
            "coverage_ratio": (duration-remaining)/duration,
            "uncovered_intervals": uncovered,
        }),
        uncovered.is_empty(),
    ))
}

pub(crate) fn display(instant: OffsetDateTime) -> String {
    let (hour, minute, second, microseconds) = instant.to_hms_micro();
    let date = instant.date();
    let mut output = format!("{date}T{hour:02}:{minute:02}:{second:02}");
    if microseconds != 0 {
        let _ = write!(output, ".{microseconds:06}");
    }
    let offset = instant.offset();
    let sign = if offset.is_negative() { '-' } else { '+' };
    let (h, m, s) = offset.as_hms();
    let _ = write!(
        output,
        "{sign}{:02}:{:02}",
        h.unsigned_abs(),
        m.unsigned_abs()
    );
    if s != 0 {
        let _ = write!(output, ":{:02}", s.unsigned_abs());
    }
    output
}

pub(crate) async fn elevated_windows(
    txn: &mut Transaction<'_, Postgres>,
    left: OffsetDateTime,
    right: OffsetDateTime,
) -> Result<Vec<(OffsetDateTime, OffsetDateTime)>, sqlx::Error> {
    sqlx::query_as(include_str!("context/stress_windows.sql"))
        .bind(left)
        .bind(right)
        .fetch_all(&mut **txn)
        .await
}

pub(crate) async fn measurements(
    txn: &mut Transaction<'_, Postgres>,
    now: OffsetDateTime,
    left: OffsetDateTime,
    right: OffsetDateTime,
    timezone: &str,
) -> Result<Option<(f64, i64, f64)>, DiaryWriteError> {
    if right > now
        || right <= left
        || !elevated_windows(txn, left, right)
            .await?
            .contains(&(left, right))
    {
        return Ok(None);
    }
    Ok(sqlx::query_as(include_str!("context/physiology.sql"))
        .bind(now)
        .bind(left)
        .bind(right)
        .bind(timezone)
        .fetch_optional(&mut **txn)
        .await?)
}
