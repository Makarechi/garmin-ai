mod model;
mod send;

pub use model::{
    WebChatInterpretationContext, WebChatModelPreparation, WebChatModelReply, WebChatModelSession,
};
pub use send::WebChatSubmission;

use std::{collections::VecDeque, error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde::Serialize;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness,
    delivery::{DeliveryReceipt, DeliveryState, DeliveryTarget, record_delivery_receipt},
    replay::REPLAY_MUTATION_LOCK,
    share_policy::{CHANNEL_CONSENT_LOCK, intent_sharing_allowed},
};

const DESTINATION: &str = "web:local";
const MAX_MESSAGES: i64 = 100;

#[derive(Debug)]
pub enum WebChatError {
    Storage(ReadinessError),
    Database,
    Missing,
    Unavailable,
    Invalid,
    ReusedMessage,
}

impl fmt::Display for WebChatError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(ReadinessError::StorageDisabled) => "Storage disabled after erasure",
            Self::Storage(_) | Self::Database => "Database unavailable or identity is not ready",
            Self::Missing => "Message not found",
            Self::Unavailable => "Message is no longer available",
            Self::Invalid => "Invalid arguments",
            Self::ReusedMessage => "Message ID was already used for different text",
        })
    }
}
impl Error for WebChatError {}
impl From<sqlx::Error> for WebChatError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

#[derive(Serialize)]
struct WebChatMessage {
    id: String,
    text: Option<String>,
    created_at: String,
}

#[derive(Serialize)]
struct WebChatReply {
    id: String,
    inbound_message_id: Option<String>,
    state: String,
    created_at: String,
}

/// Keep channel permissions stable while the response is released by the caller.
pub struct WebChatSnapshot {
    opening: Option<Vec<u8>>,
    replies: VecDeque<(Uuid, i32, WebChatReply)>,
    active: Option<(Uuid, i32, i32)>,
    first: bool,
    closing: Option<&'static [u8]>,
    permissions: Transaction<'static, Postgres>,
}

impl WebChatSnapshot {
    /// Keep response memory bounded even when a saved reply contains many text blocks.
    pub async fn next_json(&mut self) -> Result<Option<Vec<u8>>, WebChatError> {
        if let Some(opening) = self.opening.take() {
            return Ok(Some(opening));
        }
        if let Some((id, index, count)) = self.active {
            if index == count {
                self.active = None;
                return Ok(Some(b"\"}".to_vec()));
            }
            let text: String = sqlx::query_scalar(
                "SELECT intent #>> ARRAY['blocks',$2::text,'text'] FROM outbox_messages WHERE id=$1",
            ).bind(id).bind(index).fetch_one(&mut *self.permissions).await?;
            let mut fragment = serde_json::to_vec(&text).map_err(|_| WebChatError::Database)?;
            fragment.pop();
            if index == 0 {
                fragment.remove(0);
            } else {
                fragment[0] = b'\\';
                fragment.insert(1, b'n');
            }
            self.active = Some((id, index + 1, count));
            return Ok(Some(fragment));
        }
        if let Some((id, count, header)) = self.replies.pop_front() {
            let mut bytes = if self.first { Vec::new() } else { vec![b','] };
            self.first = false;
            serde_json::to_writer(&mut bytes, &header).map_err(|_| WebChatError::Database)?;
            bytes.pop();
            bytes.extend_from_slice(b",\"text\":\"");
            self.active = Some((id, 0, count));
            return Ok(Some(bytes));
        }
        Ok(self.closing.take().map(<[u8]>::to_vec))
    }
}

#[derive(Clone, Copy)]
struct Conversation {
    owner: Uuid,
    id: Uuid,
}

type OutboxRow = (Uuid, Option<Uuid>, String, OffsetDateTime, i32, String);

impl Database {
    /// Load the authenticated owner's existing web conversation without recording a read.
    pub async fn web_chat_messages(&self) -> Result<WebChatSnapshot, WebChatError> {
        tokio::time::timeout(Duration::from_secs(4), async {
            let mut txn = self.work.begin_with("BEGIN READ ONLY").await?;
            let conversation = prepare(&mut txn, false).await?;
            let messages = sqlx::query_as::<_, (Uuid, Option<String>, OffsetDateTime)>(
                "SELECT id,normalized_text,received_at FROM inbound_messages
                 WHERE owner_id=$1 AND conversation_id=$2 AND channel='web' AND channel_instance_id='local'
                 ORDER BY received_at DESC,id DESC LIMIT $3",
            )
            .bind(conversation.owner).bind(conversation.id).bind(MAX_MESSAGES)
            .fetch_all(&mut *txn).await?
            .into_iter().rev().map(|(id,text,created)| Ok(WebChatMessage {
                id:id.to_string(), text, created_at: timestamp(created)?.to_string(),
            })).collect::<Result<Vec<_>, WebChatError>>()?;
            let recorded = sqlx::query_as::<_, OutboxRow>(
                "SELECT id,inbound_message_id,state,created_at,
                        CASE WHEN jsonb_typeof(intent->'blocks')='array' THEN jsonb_array_length(intent->'blocks') ELSE 0 END,
                        (intent - ARRAY['blocks','attachments','form','actions'])::text
                 FROM outbox_messages
                 WHERE owner_id=$1 AND conversation_id=$2 AND state IN ('queued','read')
                   AND (state='read' OR coalesce(intent->'initiative','false')='false')
                 ORDER BY created_at DESC,id DESC LIMIT $3",
            )
            .bind(conversation.owner).bind(conversation.id).bind(MAX_MESSAGES)
            .fetch_all(&mut *txn).await?;
            let mut replies = VecDeque::with_capacity(recorded.len());
            for (id, inbound, state, at, count, raw) in recorded.into_iter().rev() {
                if let Some(document) = binding_document(conversation, id, &raw)
                    && can_show(&mut txn, &state, &document).await?
                {
                    replies.push_back((id, count, WebChatReply {
                        id:id.to_string(), inbound_message_id:inbound.map(|id| id.to_string()),
                        state, created_at:timestamp(at)?.to_string(),
                    }));
                }
            }
            let mut opening = b"{\"messages\":".to_vec();
            serde_json::to_writer(&mut opening, &messages).map_err(|_| WebChatError::Database)?;
            opening.extend_from_slice(b",\"replies\":[");
            Ok(WebChatSnapshot {
                opening:Some(opening), replies, active:None, first:true, closing:Some(b"]}"), permissions:txn,
            })
        }).await.map_err(|_| WebChatError::Database)?
    }

    /// Acknowledge a displayed reply and persist evidence once.
    pub async fn mark_web_chat_read(&self, outbox: Uuid) -> Result<(), WebChatError> {
        tokio::time::timeout(Duration::from_secs(3), async {
            let mut txn = self.work.begin().await?;
            let conversation = prepare(&mut txn, true).await?;
            let (inbound, state, raw): (Option<Uuid>, String, String) = sqlx::query_as(
                "SELECT inbound_message_id,state,(intent - ARRAY['blocks','attachments','form','actions'])::text
                 FROM outbox_messages
                 WHERE id=$1 AND owner_id=$2 AND conversation_id=$3 FOR UPDATE",
            )
            .bind(outbox).bind(conversation.owner).bind(conversation.id)
            .fetch_optional(&mut *txn).await?.ok_or(WebChatError::Missing)?;
            let document = binding_document(conversation, outbox, &raw).ok_or(WebChatError::Missing)?;
            if inbound.is_none() && document["initiative"] != true {
                return Err(WebChatError::Missing);
            }
            if !matches!(state.as_str(), "queued" | "read")
                || !can_show(&mut txn, &state, &document).await?
            {
                return Err(WebChatError::Unavailable);
            }
            if state == "queued" {
                let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                    .fetch_one(&mut *txn).await?;
                record_delivery_receipt(&mut txn, DeliveryTarget {
                    owner_id:conversation.owner, outbox_id:outbox, lease_token:None,
                }, DeliveryReceipt {
                    id:Uuid::new_v4(), intent_id:outbox,state:DeliveryState::Read,
                    observed_at:timestamp(at)?,provider_reference:None,detail:None,
                }).await.map_err(|_| WebChatError::Database)?;
            }
            if let Some(inbound) = inbound {
                sqlx::query(
                    "INSERT INTO app_state(key,value)
                     SELECT concat('outbox:web:local:update:',external_event_id,':0'),'{\"status\":\"sent\"}'
                     FROM inbound_messages WHERE id=$1 AND owner_id=$2 AND conversation_id=$3
                       AND channel='web' AND channel_instance_id='local'
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ).bind(inbound).bind(conversation.owner).bind(conversation.id).execute(&mut *txn).await?;
            }
            txn.commit().await?;
            Ok(())
        }).await.map_err(|_| WebChatError::Database)?
    }
}

async fn prepare(
    txn: &mut Transaction<'_, Postgres>,
    writing: bool,
) -> Result<Conversation, WebChatError> {
    sqlx::query("SET LOCAL statement_timeout='750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn).await.map_err(WebChatError::Storage)?;
    let action = if writing {
        "SELECT pg_advisory_xact_lock($1)"
    } else {
        "SELECT pg_advisory_xact_lock_shared($1)"
    };
    // Brief local work can overlap a duplicate's final read. The statement
    // timeout bounds the wait, and model calls hold neither of these locks.
    sqlx::query(action)
        .bind(REPLAY_MUTATION_LOCK)
        .execute(&mut **txn)
        .await?;
    let consent: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
        .bind(CHANNEL_CONSENT_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !consent {
        return Err(WebChatError::Database);
    }
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&mut **txn)
        .await?;
    Ok(Conversation {
        owner,
        id: Uuid::new_v5(
            &Uuid::NAMESPACE_URL,
            format!("garmin-ai/web-chat/{owner}").as_bytes(),
        ),
    })
}

fn timestamp(at: OffsetDateTime) -> Result<StoredInstant, WebChatError> {
    StoredInstant::try_from(at).map_err(|_| WebChatError::Database)
}

fn binding_document(conversation: Conversation, id: Uuid, raw: &str) -> Option<Value> {
    let document: Value = serde_json::from_str(raw).ok()?;
    (document["intent_id"] == id.to_string()
        && document["owner_id"] == conversation.owner.to_string()
        && document["conversation_id"] == conversation.id.to_string()
        && document["channel_instance"]["channel"] == "web"
        && document["channel_instance"]["instance_id"] == "local")
        .then_some(document)
}

async fn can_show(
    txn: &mut Transaction<'_, Postgres>,
    state: &str,
    document: &Value,
) -> Result<bool, WebChatError> {
    if (state == "queued" && document["initiative"] == true)
        || document["evidence_refs"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .any(|value| {
                value.starts_with("sleep-timing:") || value.starts_with("analysis-sleep-timing:")
            })
    {
        return Ok(false);
    }
    intent_sharing_allowed(txn, DESTINATION, document, &[])
        .await
        .map_err(|_| WebChatError::Database)
}
