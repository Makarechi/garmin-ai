use std::time::Duration;

use futures_util::TryStreamExt;
use ga_domain::{
    event_input::{local_wall_time_after, source_calendar_wall_time},
    timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::{OffsetDateTime, Time};

use super::{NotificationExclusions, ProactiveControlError, budget::usage_in, lock_policy};
use crate::{
    Database,
    channel_ingress::native_telegram,
    check_readiness,
    diary::{ClarificationClock, clarification_active_at},
    replay::REPLAY_MUTATION_LOCK,
};

#[derive(Clone, Copy)]
pub struct NotificationPolicy<'a> {
    pub timezone: &'a str,
    pub default_enabled: bool,
    pub daily_budget: Option<u8>,
    pub quiet_start: Time,
    pub quiet_end: Time,
    pub snoozed_until: Option<StoredInstant>,
    pub destination: Option<&'a str>,
    pub exclusions: NotificationExclusions<'a>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NotificationReason {
    OwnerPaused,
    Snoozed,
    InboundPending,
    ClarificationPending,
    TrackerSetupPending,
    DailyBudgetExhausted,
    QuietHours,
}

impl NotificationReason {
    pub fn label(self) -> &'static str {
        match self {
            Self::OwnerPaused => "owner_paused",
            Self::Snoozed => "snoozed",
            Self::InboundPending => "inbound_pending",
            Self::ClarificationPending => "clarification_pending",
            Self::TrackerSetupPending => "tracker_setup_pending",
            Self::DailyBudgetExhausted => "daily_budget_exhausted",
            Self::QuietHours => "quiet_hours",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NotificationDecision {
    Allow,
    Cancel(NotificationReason),
    Defer {
        reason: NotificationReason,
        retry_at: StoredInstant,
    },
}

impl Database {
    /// Evaluate current owner controls and shared usage without releasing any notification.
    pub async fn notification_decision(
        &self,
        settings: &NotificationPolicy<'_>,
        now: StoredInstant,
    ) -> Result<NotificationDecision, ProactiveControlError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin().await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(ProactiveControlError::Storage)?;
            lock_notification_policy(&mut txn).await?;
            let decision = decision_in(&mut txn, settings, now).await?;
            txn.commit().await?;
            Ok(decision)
        })
        .await
        .map_err(|_| ProactiveControlError::Database)?
    }
}

pub(super) async fn lock_notification_policy(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), ProactiveControlError> {
    // Inbound changes and clarification use the same mutation lock as diary edits.
    let stable: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !stable {
        return Err(ProactiveControlError::Busy);
    }
    lock_policy(txn).await
}

pub(super) async fn decision_in(
    txn: &mut Transaction<'_, Postgres>,
    settings: &NotificationPolicy<'_>,
    now: StoredInstant,
) -> Result<NotificationDecision, ProactiveControlError> {
    use NotificationDecision::{Allow, Cancel, Defer};
    use NotificationReason::{
        ClarificationPending, DailyBudgetExhausted, InboundPending, OwnerPaused, QuietHours,
        Snoozed, TrackerSetupPending,
    };

    if settings.daily_budget.is_some_and(|limit| limit > 20) {
        return Err(ProactiveControlError::InvalidCommand);
    }
    let paused = sqlx::query_scalar::<_, bool>(
        "SELECT value->'enabled' IS DISTINCT FROM 'true'::jsonb
         FROM app_state WHERE key='proactive:enabled'",
    )
    .fetch_optional(&mut **txn)
    .await?
    .unwrap_or(!settings.default_enabled);
    if paused {
        return Ok(Cancel(OwnerPaused));
    }
    if let Some(retry_at) = settings.snoozed_until.filter(|retry| *retry > now) {
        return Ok(Defer {
            reason: Snoozed,
            retry_at,
        });
    }
    let inbound_pending: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM telegram_updates WHERE status='pending') OR EXISTS(
           SELECT 1 FROM jobs j JOIN inbound_messages i
             ON j.dedup_key=concat('telegram:inbound:native:',i.id)
            AND j.payload->>'inbound_message_id'=i.id::text
           WHERE j.kind=$1 AND j.status IN ('pending','running') AND i.channel='telegram'
             AND i.status='pending' AND i.legacy_telegram_update_id IS NULL
         )",
    )
    .bind(native_telegram::KIND)
    .fetch_one(&mut **txn)
    .await?;
    let pending = if inbound_pending {
        Some(InboundPending)
    } else if clarification_pending(txn, settings.destination, now).await? {
        Some(ClarificationPending)
    } else if tracker_setup_pending(txn, settings.destination, now).await? {
        Some(TrackerSetupPending)
    } else {
        None
    };
    if let Some(reason) = pending {
        return Ok(Defer {
            reason,
            retry_at: OffsetDateTime::from(now)
                .checked_add(time::Duration::minutes(15))
                .ok_or(ProactiveControlError::InvalidCommand)?
                .try_into()
                .map_err(|_| ProactiveControlError::InvalidCommand)?,
        });
    }
    if let Some(limit) = settings.daily_budget
        && usage_in(txn, now, settings.timezone, settings.exclusions).await? >= i64::from(limit)
    {
        return Ok(Cancel(DailyBudgetExhausted));
    }
    if let Some(retry_at) = quiet_until(settings, now)? {
        return Ok(Defer {
            reason: QuietHours,
            retry_at,
        });
    }
    Ok(Allow)
}

async fn clarification_pending(
    txn: &mut Transaction<'_, Postgres>,
    destination: Option<&str>,
    now: StoredInstant,
) -> Result<bool, ProactiveControlError> {
    let mut rows = sqlx::query_as::<_, (String, String)>(
        "SELECT key,value::text FROM app_state
         WHERE (key='conversation:pending' OR key LIKE 'conversation:pending:%')
         AND ($1::text IS NULL OR key=$1)",
    )
    .bind(destination.map(|scope| match scope {
        "telegram:primary" => "conversation:pending".into(),
        _ => format!("conversation:pending:{scope}"),
    }))
    .fetch(&mut **txn);
    let clock = ClarificationClock {
        message_at: now,
        conversation_now: now,
        use_message_time: false,
    };
    while let Some((key, value)) = rows.try_next().await? {
        let scope = match key.strip_prefix("conversation:pending:") {
            Some("telegram:primary") => continue,
            Some(scope) => scope,
            None => "telegram:primary",
        };
        let value =
            serde_json::from_str(&value).map_err(|_| ProactiveControlError::InvalidStoredState)?;
        if clarification_active_at(&value, scope, clock) {
            return Ok(true);
        }
    }
    Ok(false)
}

async fn tracker_setup_pending(
    txn: &mut Transaction<'_, Postgres>,
    destination: Option<&str>,
    now: StoredInstant,
) -> Result<bool, ProactiveControlError> {
    let mut rows = sqlx::query_as::<_, (String, OffsetDateTime)>(
        "SELECT value::text, updated_at FROM app_state WHERE key LIKE 'tracker:chat-setup:%'
         AND ($1::text IS NULL OR key=$1)",
    )
    .bind(destination.map(|scope| format!("tracker:chat-setup:{scope}")))
    .fetch(&mut **txn);
    let now = OffsetDateTime::from(now);
    while let Some((value, updated)) = rows.try_next().await? {
        let value: Value =
            serde_json::from_str(&value).map_err(|_| ProactiveControlError::InvalidStoredState)?;
        if StoredInstant::from_legacy_input(&value["started_at"])
            .map(OffsetDateTime::from)
            .is_some_and(|started| started > now)
        {
            continue;
        }
        let saved = &value["last_activity_at"];
        let activity = match saved {
            Value::Null => Some(updated),
            Value::String(stamp) if stamp.is_empty() => Some(updated),
            saved => StoredInstant::from_legacy_input(saved).map(OffsetDateTime::from),
        };
        if activity.is_some_and(|activity| activity >= now - time::Duration::hours(24)) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn quiet_until(
    config: &NotificationPolicy<'_>,
    now: StoredInstant,
) -> Result<Option<StoredInstant>, ProactiveControlError> {
    let invalid = || ProactiveControlError::InvalidCommand;
    let (day, clock) = source_calendar_wall_time(now, config.timezone).ok_or_else(invalid)?;
    let start = config.quiet_start;
    let end = config.quiet_end;
    let quiet = if start > end {
        clock >= start || clock < end
    } else {
        (start..end).contains(&clock)
    };
    if !quiet {
        return Ok(None);
    }
    let end_day = if clock >= end {
        day.next_day().ok_or_else(invalid)?
    } else {
        day
    };
    local_wall_time_after(end_day, end, config.timezone, now)
        .map(Some)
        .ok_or_else(invalid)
}
