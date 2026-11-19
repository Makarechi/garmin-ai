use serde::Deserialize;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::{Date, format_description::well_known::Iso8601};
use uuid::Uuid;

use super::{Conversation, DESTINATION, WebChatError};
use crate::{
    conversation_memory::CONTEXT_DELIVERY_LOCK,
    health_analysis::{main_sleep_timing_in, permits_timing_in, sleep_timing_result_hash},
    personal_goals::{self, GOAL_POLICY_LOCK},
    replay::{GARMIN_PARSER_VERSION, REPLAY_MUTATION_LOCK, projection_pending},
    share_policy::{OWNER_ONBOARDING_LOCK, SharePolicyError, try_shared_lock},
};

pub(super) const SLEEP_REFERENCE: &str = "sleep-timing:";
pub(super) const DEPENDENT_REFERENCE: &str = "analysis-sleep-timing:";

#[derive(Deserialize)]
struct TimingRecord {
    projection: serde_json::Map<String, Value>,
    epoch: Option<String>,
    goals_revision: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TimingSnapshot {
    day: String,
    timezone: String,
    hash: String,
}

pub(super) fn evidence_refs(document: &Value) -> impl Iterator<Item = &str> {
    document["evidence_refs"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
}

pub(super) async fn can_show_timing(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Conversation,
    reference: &str,
) -> Result<bool, WebChatError> {
    let dependent = reference.starts_with(DEPENDENT_REFERENCE);
    let prefix = if dependent {
        DEPENDENT_REFERENCE
    } else {
        SLEEP_REFERENCE
    };
    let Some(identity) = reference.strip_prefix(prefix) else {
        return Ok(false);
    };
    if !Uuid::parse_str(identity).is_ok_and(|id| id.to_string() == identity) {
        return Ok(false);
    }
    // Like the Python publisher, dependent evidence needs a complete replay fence.
    if dependent
        && !sqlx::query_scalar::<_, bool>("SELECT pg_try_advisory_xact_lock($1)")
            .bind(REPLAY_MUTATION_LOCK)
            .fetch_one(&mut **txn)
            .await?
    {
        return Ok(false);
    }
    for lock in [
        OWNER_ONBOARDING_LOCK,
        CONTEXT_DELIVERY_LOCK,
        GOAL_POLICY_LOCK,
    ] {
        match try_shared_lock(txn, lock).await {
            Ok(()) => {}
            Err(SharePolicyError::Busy) => return Ok(false),
            Err(_) => return Err(WebChatError::Database),
        }
    }
    let saved: Option<String> = sqlx::query_scalar(
        "SELECT value::text FROM app_state WHERE key=$1 AND octet_length(value::text)<=20000",
    )
    .bind(reference)
    .fetch_optional(&mut **txn)
    .await?;
    let Some(saved) = saved.and_then(|raw| serde_json::from_str::<TimingRecord>(&raw).ok()) else {
        return Ok(false);
    };
    let snapshots = if dependent {
        if saved.projection.contains_key("generic_metric_dependencies") {
            return Ok(false);
        }
        let Some(snapshots) = saved
            .projection
            .get("sleep_timing_dependencies")
            .and_then(Value::as_array)
            .filter(|items| (1..=12).contains(&items.len()))
        else {
            return Ok(false);
        };
        if !replay_generation_matches(txn, saved.projection.get("generation")).await? {
            return Ok(false);
        }
        snapshots.as_slice()
    } else {
        let Some(snapshot) = saved.projection.get("sleep_timing") else {
            return Ok(false);
        };
        std::slice::from_ref(snapshot)
    };
    let epoch: Option<String> = sqlx::query_scalar(
        "SELECT (CASE WHEN value->'epochs' ? $1 THEN value->'epochs'->$1
                     ELSE value->'epoch' END)::text
         FROM app_state WHERE key='analysis:conversation'",
    )
    .bind(DESTINATION)
    .fetch_optional(&mut **txn)
    .await?
    .flatten();
    let Ok(epoch) = epoch.map_or(Ok(None), |encoded| serde_json::from_str(&encoded)) else {
        return Ok(false);
    };
    if saved.epoch != epoch {
        return Ok(false);
    }
    let Ok((goals, _)) = personal_goals::load(txn, conversation.owner).await else {
        return Ok(false);
    };
    let timezone: String =
        sqlx::query_scalar("SELECT timezone FROM people WHERE id=$1 AND singleton")
            .bind(conversation.owner)
            .fetch_one(&mut **txn)
            .await?;
    if goals.revision != saved.goals_revision
        || !permits_timing_in(txn)
            .await
            .map_err(|_| WebChatError::Database)?
    {
        return Ok(false);
    }
    for snapshot in snapshots {
        let Ok(snapshot) = TimingSnapshot::deserialize(snapshot) else {
            return Ok(false);
        };
        let Ok(day) = Date::parse(&snapshot.day, &Iso8601::DATE) else {
            return Ok(false);
        };
        if day.year() < 1 || day.to_string() != snapshot.day || snapshot.timezone != timezone {
            return Ok(false);
        }
        let current = main_sleep_timing_in(txn, day, &timezone)
            .await
            .map_err(|_| WebChatError::Database)?;
        if sleep_timing_result_hash(&current).map_err(|_| WebChatError::Database)? != snapshot.hash
        {
            return Ok(false);
        }
    }
    Ok(true)
}

async fn replay_generation_matches(
    txn: &mut Transaction<'_, Postgres>,
    generation: Option<&Value>,
) -> Result<bool, WebChatError> {
    let recorded: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='replay:generation'")
            .fetch_optional(&mut **txn)
            .await?;
    let recorded = recorded
        .map_or(Ok(Value::Null), |value| serde_json::from_str(&value))
        .map_err(|_| WebChatError::Database)?;
    Ok(&recorded == generation.unwrap_or(&Value::Null)
        && !projection_pending(txn, GARMIN_PARSER_VERSION).await?)
}
