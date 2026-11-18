use std::time::Duration;

use futures_util::TryStreamExt;
use ga_domain::{
    event_input::{local_midnight, local_wall_time, source_calendar_date},
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime, Time, format_description::well_known::Iso8601};
use uuid::Uuid;

use super::{ProactiveControlError, lock_policy};
use crate::{Database, check_readiness};

/// For an attempt already holding a slot, reconsider the budget without counting that slot again.
#[derive(Clone, Copy, Default)]
pub struct NotificationExclusions<'a> {
    pub insight_key: Option<&'a str>,
    pub outbox_id: Option<Uuid>,
}

impl Database {
    /// Count slots reserved or spent by questions, insight notices, and tracker reminders.
    pub async fn notification_usage(
        &self,
        now: StoredInstant,
        timezone: &str,
        exclusions: NotificationExclusions<'_>,
    ) -> Result<i64, ProactiveControlError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin().await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(ProactiveControlError::Storage)?;
            lock_policy(&mut txn).await?;
            let count = usage_in(&mut txn, now, timezone, exclusions).await?;
            txn.commit().await?;
            Ok(count)
        })
        .await
        .map_err(|_| ProactiveControlError::Database)?
    }
}

/// The caller holds the shared Python/Rust initiative policy lock through the decision and write.
pub(super) async fn usage_in(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
    timezone: &str,
    exclusions: NotificationExclusions<'_>,
) -> Result<i64, ProactiveControlError> {
    let day = source_calendar_date(now, timezone).ok_or(ProactiveControlError::InvalidCommand)?;
    let invalid = || ProactiveControlError::InvalidStoredState;
    let midnight = |date| local_midnight(date, timezone).map(OffsetDateTime::from);
    let start = midnight(day).ok_or_else(invalid)?;
    let end = midnight(day.next_day().ok_or_else(invalid)?).ok_or_else(invalid)?;
    let carry = midnight(
        day.checked_sub(time::Duration::days(4))
            .ok_or_else(invalid)?,
    )
    .ok_or_else(invalid)?;
    let now = OffsetDateTime::from(now);
    let questions: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pending_questions WHERE sent_at BETWEEN $1 AND $2",
    )
    .bind(start)
    .bind(now)
    .fetch_one(&mut **txn)
    .await?;
    let mut count = questions;
    {
        let mut insights = sqlx::query_scalar::<_, Option<String>>(
            "SELECT value->>'at' FROM app_state WHERE key LIKE 'insight:last:%'
             AND key IS DISTINCT FROM $1::text",
        )
        .bind(exclusions.insight_key)
        .fetch(&mut **txn);
        while let Some(at) = insights.try_next().await? {
            let at = at
                .and_then(|at| StoredInstant::from_legacy_input(&Value::String(at)))
                .map(OffsetDateTime::from)
                .ok_or_else(invalid)?;
            count += i64::from((start..=now).contains(&at));
        }
    }
    let mut outbox =
        sqlx::query_as::<_, (bool, Option<String>, Option<String>, bool)>(OUTBOX_USAGE)
            .bind(start)
            .bind(now)
            .bind(end)
            .bind(carry)
            .bind(format!(":{day}"))
            .bind(exclusions.outbox_id)
            .fetch(&mut **txn);
    while let Some((reserved, rule, day, attempted_today)) = outbox.try_next().await? {
        count += i64::from(reserved);
        if let (Some(rule), Some(day)) = (rule, day)
            && carries_on_day(&rule, &day, start, now, attempted_today)?
        {
            count += 1;
        }
    }
    Ok(count)
}

#[derive(Deserialize)]
struct CarryRule {
    rule: Schedule,
    #[serde(default = "utc")]
    timezone: String,
}

fn utc() -> String {
    "UTC".into()
}

#[derive(Deserialize)]
struct Schedule {
    kind: String,
    local_time: Option<String>,
}

fn carries_on_day(
    rule: &str,
    day: &str,
    start: OffsetDateTime,
    now: OffsetDateTime,
    attempted_today: bool,
) -> Result<bool, ProactiveControlError> {
    let invalid = || ProactiveControlError::InvalidStoredState;
    let rule: CarryRule = serde_json::from_str(rule).map_err(|_| invalid())?;
    if !matches!(rule.rule.kind.as_str(), "schedule" | "missing_entry") {
        return Ok(false);
    }
    let Ok(day) = Date::parse(day, &Iso8601::DATE) else {
        return Ok(false);
    };
    let clock = rule
        .rule
        .local_time
        .and_then(|clock| Time::parse(&clock, &Iso8601::TIME).ok())
        .ok_or_else(invalid)?;
    let scheduled = local_wall_time(day, clock, &rule.timezone)
        .map(OffsetDateTime::from)
        .ok_or_else(invalid)?;
    if scheduled >= start {
        return Ok(false);
    }
    let end = day
        .with_time(clock)
        .checked_add(time::Duration::hours(12))
        .ok_or_else(invalid)?;
    let end = local_wall_time(end.date(), end.time(), &rule.timezone)
        .map(OffsetDateTime::from)
        .ok_or_else(invalid)?;
    Ok(attempted_today || now < end)
}

const OUTBOX_USAGE: &str = r"
    WITH decisions AS (
        SELECT o.intent, o.dedup_key,
            ((o.state NOT IN ('cancelled','failed','expired') AND
                (((o.created_at >= $1 AND o.created_at < $3 OR right(o.dedup_key, 11) = $5)
                   AND (o.next_attempt_at IS NULL OR o.next_attempt_at < $3))
                  OR (o.next_attempt_at >= $1 AND o.next_attempt_at < $3)))
               OR coalesce(receipts.first_confirmed BETWEEN $1 AND $2, false)
               OR (o.attempts > 1 AND receipts.reconfirmed)) AS reserved,
            (o.state IN ('queued','sending','uncertain')
                AND o.created_at >= $4 AND o.created_at < $1
                AND (o.next_attempt_at IS NULL OR o.next_attempt_at < $1)
                AND NOT coalesce(receipts.first_confirmed BETWEEN $1 AND $2, false)
                AND NOT (o.attempts > 1 AND receipts.reconfirmed)) AS carried,
            (o.attempts > 0 AND o.state IN ('sending','uncertain') AND
                (coalesce(o.lease_until BETWEEN $1 AND $3, false)
                 OR o.updated_at BETWEEN $1 AND $2 OR receipts.uncertain_today)) AS attempted_today
        FROM outbox_messages o
        CROSS JOIN LATERAL (
            SELECT
                min(observed_at) FILTER
                  (WHERE state IN ('provider_accepted','delivered','read','uncertain'))
                  AS first_confirmed,
                coalesce(
                    max(observed_at) FILTER
                      (WHERE state IN ('provider_accepted','delivered','read')
                       AND observed_at BETWEEN $1 AND $2)
                    > max(observed_at) FILTER (WHERE state='uncertain'), false) AS reconfirmed,
                coalesce(bool_or(state='uncertain' AND observed_at BETWEEN $1 AND $2), false)
                  AS uncertain_today
            FROM message_delivery_receipts WHERE outbox_message_id=o.id
        ) receipts
        WHERE o.intent->'initiative' = 'true'::jsonb AND o.id IS DISTINCT FROM $6::uuid
    )
    SELECT coalesce(pending.reserved, false), rule.value::text,
           coalesce(nullif(pending.intent->>'scheduled_day', ''),
                    substring(pending.dedup_key FROM ':([^:]+)(?::fallback:[0-9]+)?$')),
           pending.attempted_today
    FROM decisions pending
    LEFT JOIN LATERAL (
        SELECT ref FROM jsonb_array_elements_text(
            CASE WHEN pending.carried THEN coalesce(pending.intent->'evidence_refs','[]') ELSE '[]' END
        ) WITH ORDINALITY AS refs(ref, position)
        WHERE left(ref, 5)='rule:' ORDER BY position LIMIT 1
    ) marker ON true
    LEFT JOIN app_state rule ON rule.key='initiative:' || marker.ref
    WHERE pending.reserved OR rule.key IS NOT NULL
";
