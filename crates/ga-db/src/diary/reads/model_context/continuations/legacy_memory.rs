use std::collections::BTreeMap;

use ga_domain::{
    legacy_json::{Utf8Layout, encode},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};

use super::DiaryReadError;

const MEMORY: &str = "analysis:conversation";
const PENDING: &str = "analysis:conversation:pending";
const PRIMARY: &str = "telegram:primary";
const MAX_BYTES: usize = 12_000;
const MAX_TURNS: usize = 6;
const HISTORY_AGE: i64 = 7 * 24 * 60 * 60 * 1_000_000;

pub(super) async fn recent_question(
    txn: &mut Transaction<'_, Postgres>,
    channel: &str,
    now: StoredInstant,
) -> Result<Option<Value>, DiaryReadError> {
    let saved = state(txn, MEMORY).await?;
    let pending_key = if channel == PRIMARY {
        PENDING.to_owned()
    } else {
        format!("{PENDING}:{channel}")
    };
    let pending = state(txn, &pending_key).await?;
    let epoch = saved["epochs"].get(channel).unwrap_or(&saved["epoch"]);
    let mut turns = retained(&saved, now)?;
    if pending.is_object()
        && pending["epoch"] == *epoch
        && delivered(txn, channel, &pending["turn"]).await?
    {
        let turn = pending["turn"].clone();
        turns.retain(|old| old["update_id"] != turn["update_id"]);
        turns.push(turn);
        turns.sort_by_cached_key(at);
        let promoted = json!({"epoch":saved["epoch"],"epochs":saved["epochs"],"turns":turns});
        turns = retained(&promoted, now)?;
    }
    for turn in turns.into_iter().rev() {
        if turn["channel_instance_id"].as_str().unwrap_or(PRIMARY) != channel
            || at(&turn).is_none_or(|asked| asked > now)
            || !delivered(txn, channel, &turn).await?
        {
            continue;
        }
        let asked = turn["asked_at"]
            .as_str()
            .ok_or(DiaryReadError::InvalidEvent)?;
        let question = turn["question"]
            .as_str()
            .ok_or(DiaryReadError::InvalidEvent)?;
        return Ok(Some(
            json!({"question":question.chars().take(400).collect::<String>(),"asked_at":asked}),
        ));
    }
    Ok(None)
}

fn retained(state: &Value, now: StoredInstant) -> Result<Vec<Value>, DiaryReadError> {
    let Some(turns) = state["turns"].as_array() else {
        return Ok(Vec::new());
    };
    let mut retained = Vec::new();
    let mut by_channel = BTreeMap::new();
    let empty_epochs = json!({});
    let epochs = state
        .get("epochs")
        .filter(|epochs| epochs.is_object())
        .unwrap_or(&empty_epochs);
    for turn in turns.iter().rev() {
        let Some(asked) = at(turn) else {
            continue;
        };
        if now.unix_microseconds() - asked.unix_microseconds() > HISTORY_AGE {
            continue;
        }
        let count = by_channel
            .entry(turn["channel_instance_id"].as_str().unwrap_or(PRIMARY))
            .or_insert(0);
        if *count >= MAX_TURNS {
            continue;
        }
        retained.push(turn.clone());
        let candidate = json!({"epoch":state["epoch"],"epochs":epochs,
            "turns":retained.iter().rev().collect::<Vec<_>>()});
        let raw = serde_json::to_vec(&candidate).map_err(|_| DiaryReadError::InvalidEvent)?;
        if encode(&raw, Utf8Layout::Ordered, MAX_BYTES).is_err() {
            retained.pop();
            continue;
        }
        *count += 1;
    }
    retained.reverse();
    Ok(retained)
}

fn at(turn: &Value) -> Option<StoredInstant> {
    StoredInstant::from_legacy_input(&turn["asked_at"])
}

async fn state(txn: &mut Transaction<'_, Postgres>, key: &str) -> Result<Value, DiaryReadError> {
    let raw: Option<String> = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
        .bind(key)
        .fetch_optional(&mut **txn)
        .await?;
    raw.map(|raw| serde_json::from_str(&raw).map_err(|_| DiaryReadError::InvalidEvent))
        .transpose()
        .map(|value| value.unwrap_or(Value::Null))
}

async fn delivered(
    txn: &mut Transaction<'_, Postgres>,
    channel: &str,
    turn: &Value,
) -> Result<bool, DiaryReadError> {
    let Some(update) = turn["update_id"]
        .as_str()
        .filter(|id| !id.is_empty() && !id.contains(':'))
    else {
        return Ok(false);
    };
    // Legacy Telegram turn IDs are unique in the inbound ledger. A stored receipt
    // must also match the conversation instance before its question can leave storage.
    Ok(sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state WHERE starts_with(key,$1)
         AND value->>'status'='sent'
         AND coalesce(value->>'channel_instance_id','telegram:primary')=$2)",
    )
    .bind(format!("outbox:update:{update}:"))
    .bind(channel)
    .fetch_one(&mut **txn)
    .await?)
}
