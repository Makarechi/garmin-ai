use std::{error::Error, fmt, time::Duration};

use ga_domain::{
    legacy_json::{self, Utf8Layout},
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};

use crate::{
    Database, ReadinessError, check_readiness, conversation_memory::ConversationBinding,
    replay::REPLAY_MUTATION_LOCK,
};

const TWO_HOURS_MICROS: i64 = 2 * 60 * 60 * 1_000_000;
const MAX_MESSAGES: usize = 8;
const MAX_TRANSCRIPT_BYTES: usize = 32_000;
const PRESERVED: &[&str] = &[
    "event_ids",
    "action",
    "button",
    "targets_complete",
    "optional_refinement",
    "explicit_selector",
    "selection_revision",
    "preset_recipe",
    "preset_selected_at",
    "selected_at",
    "selection_expires_at",
];

#[derive(Clone, Copy)]
pub struct ClarificationClock {
    pub message_at: StoredInstant,
    pub conversation_now: StoredInstant,
    pub use_message_time: bool,
}

pub struct ClarificationReply<'a> {
    pub clock: ClarificationClock,
    pub text: &'a str,
    pub question: &'a str,
    pub selection_prompt: Option<&'a str>,
}

#[derive(Debug)]
pub enum ClarificationError {
    Storage(ReadinessError),
    MissingConversation,
    Busy,
    InvalidRequest,
    InvalidState,
    Database,
}

impl fmt::Display for ClarificationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Clarification storage is unavailable",
            Self::MissingConversation => "Owner conversation is unavailable",
            Self::Busy => "Diary clarification is being changed",
            Self::InvalidRequest => "Diary clarification is invalid",
            Self::InvalidState => "Saved diary clarification is invalid",
            Self::Database => "Diary clarification could not be accessed",
        })
    }
}
impl Error for ClarificationError {}
impl From<sqlx::Error> for ClarificationError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Read the active pending form for one established owner conversation.
    pub async fn pending_diary_clarification(
        &self,
        binding: ConversationBinding,
        clock: ClarificationClock,
    ) -> Result<Option<Value>, ClarificationError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.work.begin_with("BEGIN READ ONLY").await?;
            prepare(&mut txn).await?;
            let (_, result) = pending_in(&mut txn, binding, clock).await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| ClarificationError::Database)?
    }

    /// Keep a new clarification in the existing per-channel history.
    pub async fn save_diary_clarification(
        &self,
        binding: ConversationBinding,
        reply: ClarificationReply<'_>,
    ) -> Result<(), ClarificationError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin().await?;
            prepare(&mut txn).await?;
            save_in(&mut txn, binding, reply).await?;
            txn.commit().await?;
            Ok(())
        })
        .await
        .map_err(|_| ClarificationError::Database)?
    }

    /// Cancel only the bound channel's pending diary form.
    pub async fn cancel_diary_clarification(
        &self,
        binding: ConversationBinding,
    ) -> Result<bool, ClarificationError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin().await?;
            prepare(&mut txn).await?;
            let removed = cancel_in(&mut txn, binding).await?;
            txn.commit().await?;
            Ok(removed)
        })
        .await
        .map_err(|_| ClarificationError::Database)?
    }
}

pub(crate) async fn save_in(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    reply: ClarificationReply<'_>,
) -> Result<(), ClarificationError> {
    if reply.text.chars().count() > 16_000
        || reply.question.chars().count() > 3000
        || reply
            .selection_prompt
            .is_some_and(|prompt| !(1..=64).contains(&prompt.len()))
    {
        return Err(ClarificationError::InvalidRequest);
    }
    lock(txn).await?;
    let scope = scope(txn, binding).await?;
    let prior = load(txn, &scope, reply.clock).await?;
    let value = append(prior.as_ref(), &scope, reply)?;
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    .bind(&scope.key)
    .bind(value.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

pub(crate) async fn cancel_in(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
) -> Result<bool, ClarificationError> {
    lock(txn).await?;
    let scope = scope(txn, binding).await?;
    let removed = sqlx::query(
        "DELETE FROM app_state WHERE key=$1
          AND coalesce(value->>'channel_instance_id','telegram:primary')=$2",
    )
    .bind(scope.key)
    .bind(scope.destination)
    .execute(&mut **txn)
    .await?;
    Ok(removed.rows_affected() == 1)
}

pub(crate) async fn clear_history_selection(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
) -> Result<(), ClarificationError> {
    lock(txn).await?;
    let scope = scope(txn, binding).await?;
    sqlx::query(
        "DELETE FROM app_state WHERE key=$1
          AND coalesce(value->>'channel_instance_id','telegram:primary')=$2
          AND value->>'action' IN ('update','close')",
    )
    .bind(scope.key)
    .bind(scope.destination)
    .execute(&mut **txn)
    .await?;
    Ok(())
}

pub(crate) async fn record_history_selection(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    mut state: Value,
) -> Result<(), ClarificationError> {
    lock(txn).await?;
    let scope = scope(txn, binding).await?;
    let fields = state
        .as_object_mut()
        .ok_or(ClarificationError::InvalidRequest)?;
    fields.insert("channel_instance_id".into(), json!(scope.destination));
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    .bind(scope.key)
    .bind(state.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

pub(crate) async fn clear_selected_record(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    id: uuid::Uuid,
) -> Result<(), ClarificationError> {
    lock(txn).await?;
    let scope = scope(txn, binding).await?;
    sqlx::query(
        "DELETE FROM app_state WHERE key=$1
          AND coalesce(value->>'channel_instance_id','telegram:primary')=$2
          AND value->'event_ids' ? $3",
    )
    .bind(scope.key)
    .bind(scope.destination)
    .bind(id.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn prepare(txn: &mut Transaction<'_, Postgres>) -> Result<(), ClarificationError> {
    sqlx::query("SET LOCAL statement_timeout='750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn)
        .await
        .map_err(ClarificationError::Storage)
}

async fn lock(txn: &mut Transaction<'_, Postgres>) -> Result<(), ClarificationError> {
    let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if acquired {
        Ok(())
    } else {
        Err(ClarificationError::Busy)
    }
}

struct Scope {
    key: String,
    destination: String,
}

pub(super) async fn pending_in(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    clock: ClarificationClock,
) -> Result<(String, Option<Value>), ClarificationError> {
    let scope = scope(txn, binding).await?;
    let pending = load(txn, &scope, clock).await?;
    Ok((scope.destination, pending))
}

async fn scope(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
) -> Result<Scope, ClarificationError> {
    let (channel, instance): (String, String) = sqlx::query_as(
        "SELECT channel,channel_instance_id FROM conversations WHERE id=$1 AND owner_id=$2",
    )
    .bind(binding.conversation_id)
    .bind(binding.owner_id)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(ClarificationError::MissingConversation)?;
    // Integration destinations have independent, exact channel namespaces.
    if channel.is_empty() || channel.contains(':') || instance.is_empty() {
        return Err(ClarificationError::InvalidState);
    }
    let destination = format!("{channel}:{instance}");
    let key = if destination == "telegram:primary" {
        "conversation:pending".into()
    } else {
        format!("conversation:pending:{destination}")
    };
    Ok(Scope { key, destination })
}

async fn load(
    txn: &mut Transaction<'_, Postgres>,
    scope: &Scope,
    clock: ClarificationClock,
) -> Result<Option<Value>, ClarificationError> {
    let row: Option<String> = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
        .bind(&scope.key)
        .fetch_optional(&mut **txn)
        .await?;
    let Some(row) = row else { return Ok(None) };
    let value: Value = serde_json::from_str(&row).map_err(|_| ClarificationError::InvalidState)?;
    Ok(clarification_active_at(&value, &scope.destination, clock).then_some(value))
}

pub(crate) fn clarification_active_at(
    value: &Value,
    scope: &str,
    clock: ClarificationClock,
) -> bool {
    let Some(fields) = value.as_object() else {
        return false;
    };
    let destination = fields
        .get("channel_instance_id")
        .map_or(Some("telegram:primary"), Value::as_str);
    if destination != Some(scope) {
        return false;
    }
    let Some(created) = fields.get("created_at") else {
        return false;
    };
    let Some(at) = fields
        .get("preset_selected_at")
        .unwrap_or(created)
        .as_str()
        .and_then(|text| text.parse::<StoredInstant>().ok())
    else {
        return false;
    };
    let message_clock =
        clock.use_message_time || fields.get("explicit_selector") == Some(&Value::Bool(true));
    let now = if message_clock {
        clock.message_at
    } else {
        clock.conversation_now
    };
    (0..=TWO_HOURS_MICROS).contains(&(now.unix_microseconds() - at.unix_microseconds()))
}

fn append(
    prior: Option<&Value>,
    scope: &Scope,
    reply: ClarificationReply<'_>,
) -> Result<Value, ClarificationError> {
    let mut value = Map::new();
    let mut history = Vec::new();
    if let Some(prior) = prior {
        for key in PRESERVED {
            if let Some(field) = prior.get(key) {
                value.insert((*key).to_owned(), field.clone());
            }
        }
        history = prior
            .get("messages")
            .and_then(Value::as_array)
            .map(|messages| messages[messages.len().saturating_sub(MAX_MESSAGES - 1)..].to_vec())
            .unwrap_or_default();
        if history.is_empty() {
            history.push(json!({
                "text":prior.get("text").cloned().unwrap_or_else(||json!("")),
                "question":prior.get("question").cloned().unwrap_or_else(||json!("")),
                "at":prior["created_at"],
            }));
        }
        if prior["explicit_selector"] == true {
            let prompt = reply
                .selection_prompt
                .ok_or(ClarificationError::InvalidRequest)?;
            value.insert("selection_prompt".to_owned(), json!(prompt));
        }
    }
    history.push(json!({"text":reply.text,"question":reply.question,"at":reply.clock.message_at.to_string()}));
    let mut history = Value::Array(history);
    while history
        .as_array()
        .is_some_and(|messages| messages.len() > 1)
    {
        let json = serde_json::to_vec(&history).map_err(|_| ClarificationError::InvalidState)?;
        let bytes = legacy_json::encode(&json, Utf8Layout::Archive, 2 * 1024 * 1024)
            .map_err(|_| ClarificationError::InvalidState)?;
        if bytes.len() <= MAX_TRANSCRIPT_BYTES {
            break;
        }
        history
            .as_array_mut()
            .ok_or(ClarificationError::InvalidState)?
            .remove(0);
    }
    value.extend([
        ("text".to_owned(), json!(reply.text)),
        ("question".to_owned(), json!(reply.question)),
        ("messages".to_owned(), history),
        (
            "created_at".to_owned(),
            json!(reply.clock.conversation_now.to_string()),
        ),
        ("channel_instance_id".to_owned(), json!(scope.destination)),
    ]);
    Ok(Value::Object(value))
}
