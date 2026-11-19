use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, Time, format_description::well_known::Iso8601};
use uuid::Uuid;

use super::{
    Occurrence, RULE_PREFIX,
    fallback::{current_channels, plain_text, route_key},
    has_entry, onboarding_routes, rule_revision, tracker_rule_id, unique_conversation,
};
use crate::{
    channel_ingress::telegram_poll::channel_primary,
    outbox_claims::ReplyDestination,
    proactive_control::{
        NotificationDecision, NotificationExclusions, NotificationPolicy, ProactiveControlError,
        budget::usage_in, lock_policy, policy::decision_in,
    },
    share_policy::{OWNER_ONBOARDING_LOCK, try_shared_lock},
};

pub(crate) enum TrackerCheckinDecision {
    Ready,
    Cancel,
    Expire,
    Defer(StoredInstant),
}

pub(crate) enum TrackerCheckinFence {
    Browser,
    Provider,
}

/// Revalidate the recorded tracker, occurrence, recipient and owner policy immediately before sending.
pub(crate) async fn revalidate_tracker_checkin(
    txn: &mut Transaction<'_, Postgres>,
    destination: &ReplyDestination<'_>,
    outbox: Uuid,
    key: &str,
    intent: &mut Value,
    now: StoredInstant,
    fence: TrackerCheckinFence,
) -> Result<TrackerCheckinDecision, ProactiveControlError> {
    use TrackerCheckinDecision::{Cancel, Defer, Expire, Ready};
    if matches!(fence, TrackerCheckinFence::Provider) {
        lock_policy(txn).await?;
    } else {
        try_shared_lock(txn, OWNER_ONBOARDING_LOCK)
            .await
            .map_err(|_| ProactiveControlError::Busy)?;
    }
    let Some(((logical_key, route_index), id)) = route_key(key) else {
        return Ok(Cancel);
    };
    let raw: Option<String> = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
        .bind(format!("{RULE_PREFIX}{id}"))
        .fetch_optional(&mut **txn)
        .await?;
    let Some(rule) = raw.and_then(|raw| serde_json::from_str::<Value>(&raw).ok()) else {
        return Ok(Cancel);
    };
    let Some(tracker) = rule["topic"]
        .as_str()
        .and_then(|topic| topic.strip_prefix("tracker:"))
        .and_then(|tracker| tracker.parse::<Uuid>().ok())
    else {
        return Ok(Cancel);
    };
    let Some(version) = rule["definition_version_id"]
        .as_str()
        .and_then(|version| version.parse::<Uuid>().ok())
    else {
        return Ok(Cancel);
    };
    if id != tracker_rule_id(tracker)
        || rule["enabled"] != true
        || rule["consented"] != true
        || rule["rule"]["kind"] != "missing_entry"
    {
        return Ok(Cancel);
    }
    let revision = rule_revision(rule.clone())?;
    if !intent["policy_revision"].is_null() && intent["policy_revision"] != revision {
        return Ok(Cancel);
    }
    if route_index == 0 {
        if rule["conversation_id"] != intent["conversation_id"]
            || rule["primary_channel"] != intent["channel_instance"]
            || !channel_primary(txn, destination.channel, destination.instance_id).await?
        {
            return Ok(Cancel);
        }
    } else if intent["policy_revision"] != revision
        || intent["logical_notification_id"] != logical_key
        || !plain_text(intent)
        || !fallback_selected(txn, destination, &rule, intent, route_index).await?
        || !known_failed_chain(txn, outbox, logical_key, key).await?
    {
        return Ok(Cancel);
    }
    let invalid = || ProactiveControlError::InvalidStoredState;
    let timezone = rule["timezone"].as_str().ok_or_else(invalid)?;
    let schedule = parse_clock(&rule["rule"]["local_time"]).ok_or_else(invalid)?;
    let configured: Option<(Uuid, i32, String, String)> = sqlx::query_as(
        "SELECT t.definition_id,t.revision,t.reminder_time,t.reminder_timezone
         FROM tracker_configs t JOIN event_definitions d ON d.id=t.definition_id
            JOIN event_definition_versions v ON v.definition_id=d.id AND v.version=d.current_version
         WHERE t.owner_id=$1 AND t.id=$2 AND v.id=$3 AND t.reminder_enabled
            AND t.reminder_time IS NOT NULL AND t.reminder_timezone IS NOT NULL
            AND d.status='active' AND v.allowed_operations ? 'create'",
    )
    .bind(destination.owner_id)
    .bind(tracker)
    .bind(version)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((definition, tracker_revision, hhmm, saved_timezone)) = configured else {
        return Ok(Cancel);
    };
    if saved_timezone != timezone
        || parse_clock(&Value::String(format!("{hhmm}:00"))) != Some(schedule)
        || (!rule["tracker_revision"].is_null() && rule["tracker_revision"] != tracker_revision)
        || intent["blocks"] != json!([{"text":rule["rule"]["prompt"]}])
        || intent["evidence_refs"]
            != json!([format!("rule:{id}"), format!("definition:{definition}")])
    {
        return Ok(Cancel);
    }

    let Some(day) = intent["scheduled_day"]
        .as_str()
        .or_else(|| logical_key.rsplit(':').next())
        .and_then(|day| Date::parse(day, &Iso8601::DATE).ok())
    else {
        return Ok(Cancel);
    };
    let position = Occurrence::for_day(day, timezone, schedule).ok_or_else(invalid)?;
    if has_entry(txn, definition, &position).await? {
        return Ok(Cancel);
    }
    if now >= position.carry_until {
        super::skip_occurrence(txn, id, day, &revision, "service_recovery").await?;
        return Ok(Expire);
    }
    if now < position.due {
        return Ok(Defer(position.due));
    }
    let snoozed_until = match &rule["snoozed_until"] {
        Value::Null => None,
        value => Some(StoredInstant::from_legacy_input(value).ok_or_else(invalid)?),
    };
    let daily_budget = rule["daily_budget"]
        .as_u64()
        .filter(|budget| *budget <= 20)
        .ok_or_else(invalid)?;
    let target = format!("{}:{}", destination.channel, destination.instance_id);
    let policy = NotificationPolicy {
        timezone,
        default_enabled: true,
        daily_budget: None,
        snoozed_until,
        quiet_start: parse_clock(&rule["quiet_start"]).ok_or_else(invalid)?,
        quiet_end: parse_clock(&rule["quiet_end"]).ok_or_else(invalid)?,
        destination: Some(&target),
        exclusions: NotificationExclusions {
            outbox_id: Some(outbox),
            insight_key: None,
        },
    };
    let retry_at = match decision_in(txn, &policy, now).await? {
        NotificationDecision::Cancel(_) => return Ok(Cancel),
        NotificationDecision::Allow => None,
        NotificationDecision::Defer { retry_at, reason } => {
            if retry_at >= position.carry_until {
                super::skip_occurrence(txn, id, day, &revision, reason.label()).await?;
                return Ok(Expire);
            }
            Some(retry_at)
        }
    };
    let effective = retry_at.unwrap_or(now);
    if usage_in(txn, effective, timezone, policy.exclusions).await? >= daily_budget as i64 {
        return Ok(Cancel);
    }
    let expiry = StoredInstant::from_legacy_input(&intent["expires_at"]);
    if expiry.is_none_or(|until| until <= effective && until < position.carry_until) {
        intent["expires_at"] = json!(position.carry_until.to_string());
    }
    if StoredInstant::from_legacy_input(&intent["expires_at"])
        .is_none_or(|until| until <= effective)
    {
        return Ok(Expire);
    }
    Ok(retry_at.map_or(Ready, Defer))
}

async fn fallback_selected(
    txn: &mut Transaction<'_, Postgres>,
    destination: &ReplyDestination<'_>,
    rule: &Value,
    intent: &Value,
    index: usize,
) -> Result<bool, ProactiveControlError> {
    let Some(routes) = onboarding_routes(txn).await? else {
        return Ok(false);
    };
    let Some(channels) = current_channels(rule, &routes) else {
        return Ok(false);
    };
    let Some(channel) = channels.get(index).filter(|channel| {
        channel.channel == destination.channel
            && channel.instance_id == destination.instance_id
            && routes.fallback_channels.contains(channel)
    }) else {
        return Ok(false);
    };
    let conversation = unique_conversation(txn, destination.owner_id, channel).await?;
    Ok(conversation.is_some_and(|id| intent["conversation_id"] == id.to_string()))
}

async fn known_failed_chain(
    txn: &mut Transaction<'_, Postgres>,
    reference: Uuid,
    root: &str,
    key: &str,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT coalesce(bool_and(o.state='failed'),false)
         FROM outbox_messages selected JOIN outbox_messages o
            ON o.owner_id=selected.owner_id AND o.operation_id=selected.operation_id
         WHERE selected.id=$1 AND (o.dedup_key=$2 OR starts_with(o.dedup_key,$3))
            AND o.dedup_key<>$4",
    )
    .bind(reference)
    .bind(root)
    .bind(format!("{root}:fallback:"))
    .bind(key)
    .fetch_one(&mut **txn)
    .await
}

fn parse_clock(value: &Value) -> Option<Time> {
    Time::parse(value.as_str()?, &Iso8601::TIME).ok()
}
