//! Remove old transport text while preserving operation records and diary history.

mod legacy;
mod neutral;

use std::{borrow::Cow, error::Error, fmt};

use ga_domain::{
    identity::python_cursor_uuid,
    timestamp::{StoredInstant, python_isoformat_microseconds},
};
use serde::Serialize;
use serde_json::Value;
use sqlx::{Postgres, Transaction, postgres::types::PgInterval};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness, replay::REPLAY_MUTATION_LOCK,
    runtime_lease::RUNTIME_LOCK,
};

#[derive(Debug)]
pub enum RetentionError {
    Range,
    Cursor,
    Clock,
    Busy,
    Storage(ReadinessError),
    Database,
}

impl fmt::Display for RetentionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Range => "Retention requires 30–3650 days and a batch of 1–1000 updates",
            Self::Cursor => "Invalid retention cursor",
            Self::Clock => "Retention clock is outside the supported calendar",
            Self::Busy => "Stop the worker before pruning transport text",
            Self::Storage(_) => "Retention storage is unavailable",
            Self::Database => "Transport text retention could not complete",
        })
    }
}

impl Error for RetentionError {}

impl From<sqlx::Error> for RetentionError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

pub struct RetentionSelection<'a> {
    pub older_than_days: u16,
    pub limit: u16,
    pub apply: bool,
    pub cursor: Option<&'a str>,
    pub answer_cursor: Option<&'a str>,
    pub neutral_cursor: Option<&'a str>,
}

#[derive(Serialize)]
pub struct RetentionResult {
    pub applied: bool,
    pub cutoff: String,
    pub scanned: usize,
    pub eligible_updates: usize,
    pub eligible_jobs: usize,
    pub eligible_transcripts: usize,
    pub eligible_clarifications: usize,
    pub eligible_proactive_answers: usize,
    pub scanned_neutral_messages: usize,
    pub eligible_neutral_messages: usize,
    pub next_neutral_cursor: Option<String>,
    pub scanned_proactive_answers: usize,
    pub next_answer_cursor: Option<String>,
    pub batch_limit_reached: bool,
    pub next_cursor: Option<String>,
}

struct RetentionWindow {
    now: String,
    cutoff: OffsetDateTime,
    limit: i64,
    apply: bool,
}

type PgTransaction<'a> = Transaction<'a, Postgres>;

impl Database {
    pub async fn prune_telegram_text(
        &self,
        now: StoredInstant,
        selection: RetentionSelection<'_>,
    ) -> Result<RetentionResult, RetentionError> {
        if !(30..=3650).contains(&selection.older_than_days)
            || !(1..=1000).contains(&selection.limit)
        {
            return Err(RetentionError::Range);
        }
        let cutoff = OffsetDateTime::from(now)
            .checked_sub(Duration::days(i64::from(selection.older_than_days)))
            .ok_or(RetentionError::Clock)?;
        let cutoff_text = StoredInstant::try_from(cutoff)
            .map_err(|_| RetentionError::Clock)?
            .to_string();
        let answer_after = selection
            .answer_cursor
            .map(|value| python_cursor_uuid(value).ok_or(RetentionError::Cursor))
            .transpose()?;
        let legacy_after = selection.cursor.map(legacy::cursor).transpose()?;
        let neutral_after = neutral::Cursor::parse(selection.neutral_cursor)?;
        let window = RetentionWindow {
            cutoff,
            now: now.to_string(),
            limit: i64::from(selection.limit),
            apply: selection.apply,
        };
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL lock_timeout='1500ms'")
            .execute(&mut *txn)
            .await?;
        take_lock(&mut txn, RUNTIME_LOCK).await?;
        check_readiness(&mut txn)
            .await
            .map_err(RetentionError::Storage)?;
        take_lock(&mut txn, REPLAY_MUTATION_LOCK).await?;
        let neutral = neutral::prune(&mut txn, &window, neutral_after).await?;
        let legacy = legacy::prune(&mut txn, &window, legacy_after).await?;
        let clarifications = prune_clarifications(&mut txn, &window).await?;
        let answers = prune_answers(&mut txn, &window, answer_after).await?;
        txn.commit().await?;
        Ok(RetentionResult {
            applied: window.apply,
            cutoff: cutoff_text,
            scanned: legacy.scanned,
            eligible_updates: legacy.eligible,
            eligible_jobs: legacy.jobs,
            eligible_transcripts: legacy.transcripts,
            eligible_clarifications: clarifications,
            eligible_proactive_answers: answers.eligible,
            scanned_neutral_messages: neutral.scanned,
            eligible_neutral_messages: neutral.eligible,
            next_neutral_cursor: neutral.next,
            scanned_proactive_answers: answers.scanned,
            next_answer_cursor: answers.next.map(|id| id.to_string()),
            batch_limit_reached: legacy.next.is_some(),
            next_cursor: legacy.next,
        })
    }
}

async fn take_lock(txn: &mut PgTransaction<'_>, lock: i64) -> Result<(), RetentionError> {
    if !sqlx::query_scalar::<_, bool>("SELECT pg_try_advisory_xact_lock($1)")
        .bind(lock)
        .fetch_one(&mut **txn)
        .await?
    {
        return Err(RetentionError::Busy);
    }
    Ok(())
}

async fn prune_clarifications(
    txn: &mut PgTransaction<'_>,
    window: &RetentionWindow,
) -> Result<usize, RetentionError> {
    let pending: Vec<(String, Option<String>)> = sqlx::query_as(
        "SELECT key,value->>'created_at' FROM app_state WHERE starts_with(key,'conversation:pending')
         AND jsonb_typeof(value->'created_at')='string'",
    )
    .fetch_all(&mut **txn).await?;
    let expired: Vec<_> = pending
        .into_iter()
        .filter_map(|(key, date)| older(date.as_deref(), window.cutoff).then_some(key))
        .collect();
    if window.apply && !expired.is_empty() {
        sqlx::query("DELETE FROM app_state WHERE key=ANY($1)")
            .bind(&expired)
            .execute(&mut **txn)
            .await?;
    }
    Ok(expired.len())
}

struct Answers {
    scanned: usize,
    eligible: usize,
    next: Option<Uuid>,
}

async fn prune_answers(
    txn: &mut PgTransaction<'_>,
    window: &RetentionWindow,
    after: Option<Uuid>,
) -> Result<Answers, RetentionError> {
    let page: Vec<(Uuid, Option<String>)> = sqlx::query_as(
        "SELECT id,CASE WHEN jsonb_typeof(evidence->'answered_at')='string'
                       THEN evidence->>'answered_at' END
         FROM pending_questions WHERE evidence->>'answer_text' IS NOT NULL
              AND ($1::uuid IS NULL OR id>$1)
         ORDER BY id LIMIT $2 FOR UPDATE",
    )
    .bind(after)
    .bind(window.limit)
    .fetch_all(&mut **txn)
    .await?;
    let expired: Vec<_> = page
        .iter()
        .filter_map(|(id, date)| older(date.as_deref(), window.cutoff).then_some(*id))
        .collect();
    if window.apply && !expired.is_empty() {
        sqlx::query(
            "UPDATE pending_questions SET evidence=evidence-'answer_text' WHERE id=ANY($1)",
        )
        .bind(&expired)
        .execute(&mut **txn)
        .await?;
    }
    let next = if i64::try_from(page.len()) == Ok(window.limit) {
        page.last().map(|entry| entry.0)
    } else {
        None
    };
    Ok(Answers {
        scanned: page.len(),
        eligible: expired.len(),
        next,
    })
}

fn older(text: Option<&str>, cutoff: OffsetDateTime) -> bool {
    text.and_then(python_isoformat_microseconds)
        .is_some_and(|instant| i128::from(instant) * 1_000 < cutoff.unix_timestamp_nanos())
}

fn cursor_interval(microseconds: i64) -> PgInterval {
    // PostgreSQL accepts cursor instants outside Rust's storage calendar. An
    // interval from the UTC epoch binds the exact microsecond without a float.
    PgInterval {
        months: 0,
        days: 0,
        microseconds,
    }
}

fn parse_cursor(value: &str) -> Result<Value, RetentionError> {
    if value.chars().count() > 512 {
        return Err(RetentionError::Cursor);
    }
    serde_json::from_str(value).map_err(|_| RetentionError::Cursor)
}

fn cursor_pair(value: &Value) -> Option<(&str, Cow<'_, Value>)> {
    // Python unpacking also accepts a two-key object, in insertion order.
    match value {
        Value::Array(values) => {
            let [stamp, id] = values.as_slice() else {
                return None;
            };
            Some((stamp.as_str()?, Cow::Borrowed(id)))
        }
        Value::Object(values) if values.len() == 2 => {
            let mut keys = values.keys();
            Some((
                keys.next()?,
                Cow::Owned(Value::String(keys.next()?.clone())),
            ))
        }
        _ => None,
    }
}

fn stamp(at: OffsetDateTime) -> Result<String, RetentionError> {
    StoredInstant::try_from(at)
        .map(|value| value.to_string())
        .map_err(|_| RetentionError::Clock)
}
