use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, Time, format_description::well_known::Iso8601};
use uuid::Uuid;

use super::{Occurrence, RULE_PREFIX, has_entry, rule_revision, tracker_rule_id};
use crate::{
    channel_ingress::telegram_poll::channel_primary,
    outbox_claims::ReplyDestination,
    proactive_control::{
        NotificationDecision, NotificationExclusions, NotificationPolicy, ProactiveControlError,
        budget::usage_in, lock_policy, policy::decision_in,
    },
};

pub(crate) enum TrackerCheckinDecision {
    Ready,
    Cancel,
    Expire,
    Defer(StoredInstant),
}

/// Revalidate the recorded tracker, occurrence, recipient and owner policy immediately before sending.
pub(crate) async fn revalidate_tracker_checkin(
    txn: &mut Transaction<'_, Postgres>,
    destination: &ReplyDestination<'_>,
    outbox: Uuid,
    key: &str,
    intent: &mut Value,
    now: StoredInstant,
) -> Result<TrackerCheckinDecision, ProactiveControlError> {
    use TrackerCheckinDecision::{Cancel, Defer, Expire, Ready};
    lock_policy(txn).await?;
    let Some(id) = key
        .strip_prefix("rule:")
        .and_then(|key| key.split(':').next())
        .and_then(|id| id.parse::<Uuid>().ok())
    else {
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
        || rule["conversation_id"] != intent["conversation_id"]
        || rule["primary_channel"] != intent["channel_instance"]
        || !channel_primary(txn, destination.channel, destination.instance_id).await?
    {
        return Ok(Cancel);
    }
    let revision = rule_revision(rule.clone())?;
    if !intent["policy_revision"].is_null() && intent["policy_revision"] != revision {
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

    let logical_key = key.split(":fallback:").next().unwrap_or(key);
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

fn parse_clock(value: &Value) -> Option<Time> {
    Time::parse(value.as_str()?, &Iso8601::TIME).ok()
}
