use std::time::Duration;

use ga_domain::{
    channel::{InboundEnvelope, InboundKind},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{ExistingMessage, InboundReceipt, IngressError, ingest};
use crate::{
    Database,
    action_tokens::{ActionTokenContext, consume_action_token},
    jobs::telegram_inbound::resolve_legacy_history,
};

pub struct ChannelCallback<'a> {
    pub owner_id: Uuid,
    pub conversation_id: Uuid,
    pub channel: &'a str,
    pub instance_id: &'a str,
    pub event_id: &'a str,
    pub token: &'a str,
    pub received_at: StoredInstant,
}

impl Database {
    /// Resolve a button and persist its authenticated event in the same transaction.
    /// Repeated delivery returns the saved event before trying to consume its spent token.
    pub async fn ingest_channel_callback(
        &self,
        callback: ChannelCallback<'_>,
        normalize: impl FnOnce(&Value) -> Option<InboundEnvelope>,
    ) -> Result<InboundReceipt, IngressError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_ingress().await?;
            let receipt = resolve_and_ingest(&mut txn, &callback, normalize).await?;
            txn.commit().await?;
            Ok(receipt)
        })
        .await
        .map_err(|_| IngressError::Database)?
    }
}

pub(super) async fn resolve_and_ingest(
    txn: &mut Transaction<'_, Postgres>,
    callback: &ChannelCallback<'_>,
    normalize: impl FnOnce(&Value) -> Option<InboundEnvelope>,
) -> Result<InboundReceipt, IngressError> {
    let existing: Option<ExistingMessage> = sqlx::query_as(
        "SELECT id,operation_id,supersedes_id,owner_id,conversation_id FROM inbound_messages
         WHERE channel=$1 AND channel_instance_id=$2 AND external_event_id=$3 AND revision=1",
    )
    .bind(callback.channel)
    .bind(callback.instance_id)
    .bind(callback.event_id)
    .fetch_optional(&mut **txn)
    .await?;
    if let Some((message_id, operation_id, supersedes_id, owner_id, conversation_id)) = existing {
        if (owner_id, conversation_id) != (callback.owner_id, callback.conversation_id) {
            return Err(IngressError::ForeignConversation);
        }
        return Ok(InboundReceipt {
            message_id,
            operation_id,
            supersedes_id,
            created: false,
        });
    }
    let action = consume_outbox_action(txn, callback).await?;
    let normalized = normalize(&action).ok_or(IngressError::InvalidEnvelope)?;
    let binding = normalized.channel();
    if normalized.kind() != InboundKind::Action
        || normalized.revision() != 1
        || normalized.owner_id() != callback.owner_id
        || normalized.conversation_id() != callback.conversation_id
        || binding.channel() != callback.channel
        || binding.instance_id() != callback.instance_id
        || normalized.external_event_id() != callback.event_id
        || normalized.received_at() != OffsetDateTime::from(callback.received_at)
        || normalized.document()["action"] != action
    {
        return Err(IngressError::InvalidEnvelope);
    }
    ingest(txn, &normalized).await
}

type ActionOutbox = (Uuid, Uuid, Option<Uuid>, String);

async fn consume_outbox_action(
    txn: &mut Transaction<'_, Postgres>,
    callback: &ChannelCallback<'_>,
) -> Result<Value, IngressError> {
    if let Some(history) = resolve_legacy_history(txn, callback).await? {
        return Ok(history);
    }
    if callback.channel == "telegram"
        && callback
            .token
            .strip_prefix("create:")
            .is_some_and(|id| id.parse::<Uuid>().is_ok())
    {
        let update = callback
            .event_id
            .parse::<i64>()
            .map_err(|_| IngressError::InvalidEnvelope)?;
        return Ok(json!({"action_id":callback.token,"label":"Tracker",
            "operation_id":ga_domain::channel::legacy_telegram_action_id(update).to_string(),"token":null,"expires_at":null}));
    }
    if callback.token.len() != 57 || !callback.token.is_ascii() {
        return Err(IngressError::ActionUnavailable);
    }
    let rows: Vec<ActionOutbox> = sqlx::query_as(
        "SELECT o.id,o.operation_id,o.inbound_message_id,(o.intent->'actions')::text
          FROM outbox_messages o JOIN conversations c ON c.id=o.conversation_id AND c.owner_id=o.owner_id
         WHERE o.owner_id=$1 AND c.id=$2 AND c.channel=$3 AND c.channel_instance_id=$4
           AND o.intent->>'owner_id'=o.owner_id::text
           AND o.intent->>'conversation_id'=o.conversation_id::text
           AND o.intent->'channel_instance'->>'channel'=$3
           AND o.intent->'channel_instance'->>'instance_id'=$4
           AND o.intent->'actions' @> $5::jsonb
         ORDER BY o.id LIMIT 2 FOR UPDATE OF o",
    )
    .bind(callback.owner_id)
    .bind(callback.conversation_id)
    .bind(callback.channel)
    .bind(callback.instance_id)
    .bind(json!([{"token":callback.token}]).to_string())
    .fetch_all(&mut **txn)
    .await?;
    let [(outbox_id, operation, inbound, actions)] = rows.as_slice() else {
        return Err(IngressError::ActionUnavailable);
    };
    let revision = if let Some(inbound) = inbound {
        sqlx::query_scalar(
            "SELECT i.revision FROM inbound_messages i WHERE i.id=$1
                         AND i.owner_id=$2 AND i.conversation_id=$3 AND i.operation_id=$4
                         AND i.revision=(SELECT max(revision) FROM inbound_messages
                             WHERE owner_id=$2 AND conversation_id=$3 AND operation_id=$4)",
        )
        .bind(inbound)
        .bind(callback.owner_id)
        .bind(callback.conversation_id)
        .bind(operation)
        .fetch_optional(&mut **txn)
        .await?
        .ok_or(IngressError::ActionUnavailable)?
    } else {
        1
    };
    let mut actions: Vec<Value> =
        serde_json::from_str(actions).map_err(|_| IngressError::ActionUnavailable)?;
    let mut matching = actions
        .iter_mut()
        .filter(|action| action["token"].as_str() == Some(callback.token));
    let Some(action) = matching.next() else {
        return Err(IngressError::ActionUnavailable);
    };
    if matching.next().is_some()
        || action
            .get("expires_at")
            .filter(|at| !at.is_null())
            .is_some_and(|at| {
                StoredInstant::from_legacy_input(at).is_none_or(|at| at <= callback.received_at)
            })
    {
        return Err(IngressError::ActionUnavailable);
    }
    let context = ActionTokenContext {
        owner_id: callback.owner_id,
        conversation_id: callback.conversation_id,
        revision,
    };
    let confirmed = consume_action_token(txn, context, callback.token, callback.received_at)
        .await
        .map_err(|_| IngressError::Database)?
        .ok_or(IngressError::ActionUnavailable)?;
    if action["action_id"].as_str() != Some(&confirmed) {
        return Err(IngressError::ActionUnavailable);
    }
    action["token"] = Value::Null;
    let result = action.clone();
    sqlx::query(
        "UPDATE outbox_messages SET intent=jsonb_set(intent,'{actions}',$2::jsonb),updated_at=now()
          WHERE id=$1",
    )
    .bind(outbox_id)
    .bind(json!(actions).to_string())
    .execute(&mut **txn)
    .await?;
    Ok(result)
}
