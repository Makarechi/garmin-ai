mod generated;
pub use generated::queue_generated_outbox_intent;

use std::{error::Error, fmt};

use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::Value;
use sqlx::{Acquire, Postgres, Transaction};
use uuid::Uuid;

use crate::{
    ReadinessError,
    action_tokens::{ActionTokenContext, ActionTokenError, issue_action_token},
    check_readiness,
    replay::REPLAY_MUTATION_LOCK,
    share_policy::{ShareRequirement, intent_sharing_allowed},
};

const DEFAULT_ACTION_LIFETIME_MICROS: i64 = 15 * 60 * 1_000_000;

pub struct QueueIntent<'a> {
    pub intent: OutboundIntent,
    pub operation_id: Uuid,
    pub inbound_id: Option<Uuid>,
    pub dedup_key: Option<&'a str>,
    pub requirements: &'a [ShareRequirement<'a>],
    pub now: StoredInstant,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct QueuedIntent {
    pub outbox_id: Uuid,
    pub created: bool,
}

#[derive(Debug)]
pub enum QueueIntentError {
    Storage(ReadinessError),
    Busy,
    ForeignContext,
    ConsentRequired,
    Action(ActionTokenError),
    Database,
}

impl fmt::Display for QueueIntentError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Storage(_) => f.write_str("Outbox storage is unavailable"),
            Self::Busy => f.write_str("Outbox storage is busy"),
            Self::ForeignContext => {
                f.write_str("Outbox intent is outside the authenticated context")
            }
            Self::ConsentRequired => f.write_str("Sensitive tracker channel consent is required"),
            Self::Action(error) => error.fmt(f),
            Self::Database => f.write_str("Outbox intent could not be stored"),
        }
    }
}
impl Error for QueueIntentError {}
impl From<sqlx::Error> for QueueIntentError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

/// Queue a validated reply with the caller's domain writes and issue its one-use actions.
/// A retry using the same key returns the first intent without issuing new actions.
pub async fn queue_outbox_intent(
    txn: &mut Transaction<'_, Postgres>,
    request: QueueIntent<'_>,
) -> Result<QueuedIntent, QueueIntentError> {
    let mut change = txn.begin().await?;
    let result = enqueue(&mut change, request).await;
    match result {
        Ok(receipt) => {
            change.commit().await?;
            Ok(receipt)
        }
        Err(error) => {
            change.rollback().await?;
            Err(error)
        }
    }
}

async fn enqueue(
    txn: &mut Transaction<'_, Postgres>,
    request: QueueIntent<'_>,
) -> Result<QueuedIntent, QueueIntentError> {
    check_readiness(txn)
        .await
        .map_err(QueueIntentError::Storage)?;
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(QueueIntentError::Busy);
    }
    let QueueIntent {
        intent,
        operation_id,
        inbound_id,
        dedup_key,
        requirements,
        now,
    } = request;
    let owner = intent.owner_id();
    let conversation = intent.conversation_id();
    let channel = intent.channel();
    let bound: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM conversations WHERE id=$1 AND owner_id=$2
             AND channel=$3 AND channel_instance_id=$4)",
    )
    .bind(conversation)
    .bind(owner)
    .bind(channel.channel())
    .bind(channel.instance_id())
    .fetch_one(&mut **txn)
    .await?;
    if !bound {
        return Err(QueueIntentError::ForeignContext);
    }

    let target = format!("{}:{}", channel.channel(), channel.instance_id());
    if !intent_sharing_allowed(txn, &target, intent.document(), requirements)
        .await
        .map_err(|_| QueueIntentError::Database)?
    {
        return Err(QueueIntentError::ConsentRequired);
    }
    let key = dedup_key
        .map(str::to_owned)
        .unwrap_or_else(|| format!("operation:{operation_id}:reply"));
    let existing: Option<(Uuid, Uuid, Uuid, Uuid)> = sqlx::query_as(
        "SELECT id,owner_id,conversation_id,operation_id FROM outbox_messages WHERE dedup_key=$1",
    )
    .bind(&key)
    .fetch_optional(&mut **txn)
    .await?;
    if let Some((outbox_id, existing_owner, existing_conversation, existing_operation)) = existing {
        if (existing_owner, existing_conversation, existing_operation)
            != (owner, conversation, operation_id)
        {
            return Err(QueueIntentError::ForeignContext);
        }
        return Ok(QueuedIntent {
            outbox_id,
            created: false,
        });
    }
    let revision = match inbound_id {
        Some(inbound) => sqlx::query_scalar(
            "SELECT revision FROM inbound_messages WHERE id=$1 AND owner_id=$2 AND conversation_id=$3
                AND operation_id=$4 AND legacy_telegram_update_id IS NULL",
        )
        .bind(inbound).bind(owner).bind(conversation).bind(operation_id)
        .fetch_optional(&mut **txn).await?
        .ok_or(QueueIntentError::ForeignContext)?,
        None => 1,
    };
    let context = ActionTokenContext {
        owner_id: owner,
        conversation_id: conversation,
        revision,
    };
    let outbox_id = intent.intent_id();
    let mut intent = intent.into_document();
    sign_actions(txn, &mut intent, context, now).await?;

    sqlx::query(
        "INSERT INTO outbox_messages(id,owner_id,conversation_id,inbound_message_id,operation_id,
              intent,dedup_key,state,attempts) VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,'queued',0)",
    )
    .bind(outbox_id)
    .bind(owner)
    .bind(conversation)
    .bind(inbound_id)
    .bind(operation_id)
    .bind(intent.to_string())
    .bind(key)
    .execute(&mut **txn)
    .await?;
    Ok(QueuedIntent {
        outbox_id,
        created: true,
    })
}

async fn sign_actions(
    txn: &mut Transaction<'_, Postgres>,
    intent: &mut Value,
    context: ActionTokenContext,
    now: StoredInstant,
) -> Result<(), QueueIntentError> {
    let actions = intent["actions"]
        .as_array_mut()
        .ok_or(QueueIntentError::Database)?;
    for action in actions {
        let explicit = action.get("expires_at").filter(|value| !value.is_null());
        let expires = match explicit {
            Some(value) => {
                StoredInstant::from_legacy_input(value).ok_or(QueueIntentError::Database)?
            }
            None => now
                .unix_microseconds()
                .checked_add(DEFAULT_ACTION_LIFETIME_MICROS)
                .and_then(|value| StoredInstant::from_unix_microseconds(value).ok())
                .ok_or(QueueIntentError::Action(ActionTokenError::InvalidAction))?,
        };
        let label = action["action_id"]
            .as_str()
            .ok_or(QueueIntentError::Database)?;
        let token = issue_action_token(txn, context, label, now, expires)
            .await
            .map_err(QueueIntentError::Action)?;
        if explicit.is_none() {
            action["expires_at"] = Value::String(expires.to_string().replace("+00:00", "Z"));
        }
        action["token"] = Value::String(token);
    }
    Ok(())
}
