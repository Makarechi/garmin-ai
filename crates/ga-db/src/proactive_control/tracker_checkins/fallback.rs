use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ChannelRef, ProactiveControlError, RULE_PREFIX, Routes, TrackerCheckinDecision,
    revalidate_tracker_checkin, unique_conversation,
};
use crate::{
    outbox_claims::ReplyDestination,
    outbox_enqueue::{QueueIntent, QueueIntentError, queue_outbox_intent},
};

const PENDING_PREFIX: &str = "initiative:fallback:";

pub(crate) async fn remember_tracker_fallback(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    outbox: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state(key,value) SELECT $1,'true'::jsonb FROM outbox_messages
         WHERE id=$2 AND owner_id=$3 AND state='failed'
           AND EXISTS (SELECT 1 FROM app_state WHERE key='preferences:onboarding'
                       AND value->'fallback_channels'<>'[]'::jsonb)
         ON CONFLICT(key) DO NOTHING",
    )
    .bind(format!("{PENDING_PREFIX}{owner}:{outbox}"))
    .bind(outbox)
    .bind(owner)
    .execute(&mut **txn)
    .await?;
    Ok(())
}

/// Finish the durable fallback decision after the failed delivery releases its share locks.
pub(super) async fn settle_pending(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    routes: Option<&Routes>,
    now: StoredInstant,
) -> Result<(), ProactiveControlError> {
    let prefix = format!("{PENDING_PREFIX}{owner}:");
    let keys: Vec<String> = sqlx::query_scalar(
        "SELECT key FROM app_state WHERE starts_with(key,$1) ORDER BY updated_at,key LIMIT 16",
    )
    .bind(&prefix)
    .fetch_all(&mut **txn)
    .await?;
    for key in keys {
        if let Some((routes, id)) = routes.zip(
            key.strip_prefix(&prefix)
                .and_then(|id| id.parse::<Uuid>().ok()),
        ) {
            reroute(txn, owner, id, routes, now).await?;
        }
        sqlx::query("DELETE FROM app_state WHERE key=$1")
            .bind(key)
            .execute(&mut **txn)
            .await?;
    }
    Ok(())
}

async fn reroute(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    id: Uuid,
    routes: &Routes,
    now: StoredInstant,
) -> Result<(), ProactiveControlError> {
    let failed: Option<(Uuid, Uuid, String, String)> = sqlx::query_as(
        "SELECT conversation_id,operation_id,dedup_key,intent::text FROM outbox_messages
         WHERE id=$1 AND owner_id=$2 AND state='failed' AND inbound_message_id IS NULL
           AND intent->'initiative'='true'::jsonb AND legacy_key IS NULL FOR UPDATE",
    )
    .bind(id)
    .bind(owner)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((conversation, operation, key, raw)) = failed else {
        return Ok(());
    };
    let Some(((root, index), rule_id)) = route_key(&key) else {
        return Ok(());
    };
    let Some(mut intent) = serde_json::from_str::<Value>(&raw).ok().filter(plain_text) else {
        return Ok(());
    };
    let raw: Option<String> = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
        .bind(format!("{RULE_PREFIX}{rule_id}"))
        .fetch_optional(&mut **txn)
        .await?;
    let Some(rule) = raw.and_then(|value| serde_json::from_str::<Value>(&value).ok()) else {
        return Ok(());
    };
    let Some(channels) = current_channels(&rule, routes) else {
        return Ok(());
    };
    if !matches_route(channels.get(index), &intent["channel_instance"]) {
        return Ok(());
    }
    for (index, channel) in channels.iter().enumerate().skip(index + 1) {
        if !routes.fallback_channels.contains(channel) {
            continue;
        }
        let Some(target) = unique_conversation(txn, owner, channel).await? else {
            continue;
        };
        if target == conversation {
            continue;
        }
        let fallback_key = format!("{root}:fallback:{index}");
        let destination = ReplyDestination {
            owner_id: owner,
            channel: &channel.channel,
            instance_id: &channel.instance_id,
        };
        intent["intent_id"] = json!(Uuid::new_v4().to_string());
        intent["conversation_id"] = json!(target.to_string());
        intent["channel_instance"] = json!(channel);
        let decision =
            revalidate_tracker_checkin(txn, &destination, id, &fallback_key, &mut intent, now)
                .await?;
        let retry_at = match decision {
            TrackerCheckinDecision::Cancel | TrackerCheckinDecision::Expire => continue,
            TrackerCheckinDecision::Ready => None,
            TrackerCheckinDecision::Defer(until) => Some(until),
        };
        let copy = OutboundIntent::normalize(intent.clone())
            .map_err(|_| ProactiveControlError::InvalidStoredState)?;
        let queued = queue_outbox_intent(
            txn,
            QueueIntent {
                intent: copy,
                operation_id: operation,
                inbound_id: None,
                dedup_key: Some(&fallback_key),
                requirements: &[],
                now,
            },
        )
        .await;
        let receipt = match queued {
            Ok(receipt) => receipt,
            Err(QueueIntentError::ConsentRequired | QueueIntentError::ForeignContext) => continue,
            Err(QueueIntentError::Busy) => return Err(ProactiveControlError::Busy),
            Err(QueueIntentError::Storage(error)) => {
                return Err(ProactiveControlError::Storage(error));
            }
            Err(_) => return Err(ProactiveControlError::Database),
        };
        if let Some(until) = retry_at.filter(|_| receipt.created) {
            sqlx::query("UPDATE outbox_messages SET next_attempt_at=$2 WHERE id=$1")
                .bind(receipt.outbox_id)
                .bind(OffsetDateTime::from(until))
                .execute(&mut **txn)
                .await?;
        }
        break;
    }
    Ok(())
}

pub(super) fn route_key(key: &str) -> Option<((&str, usize), Uuid)> {
    let id = key.strip_prefix("rule:")?.split(':').next()?.parse().ok()?;
    let (root, index) = match key.split_once(":fallback:") {
        Some((root, index)) => (
            root,
            index
                .parse::<usize>()
                .ok()
                .filter(|index| (1..=3).contains(index))?,
        ),
        None => (key, 0),
    };
    Some(((root, index), id))
}

pub(super) fn current_channels(rule: &Value, routes: &Routes) -> Option<Vec<ChannelRef>> {
    let primary = routes.channel.as_ref()?;
    if !matches_route(Some(primary), &rule["primary_channel"]) {
        return None;
    }
    let fallbacks = rule["fallback_channels"].as_array()?;
    if fallbacks.len() > 3 {
        return None;
    }
    let mut channels = Vec::with_capacity(fallbacks.len() + 1);
    channels.push(primary.clone());
    for channel in fallbacks {
        channels.push(serde_json::from_value(channel.clone()).ok()?);
    }
    Some(channels)
}

fn matches_route(channel: Option<&ChannelRef>, value: &Value) -> bool {
    channel.is_some_and(|channel| {
        value["channel"] == channel.channel && value["instance_id"] == channel.instance_id
    })
}

pub(super) fn plain_text(intent: &Value) -> bool {
    intent["preferred_medium"] == "text"
        && ["reply_to", "replaces", "form"]
            .iter()
            .all(|name| intent[*name].is_null())
        && ["actions", "attachments"]
            .iter()
            .all(|name| intent[*name].as_array().is_some_and(Vec::is_empty))
}
