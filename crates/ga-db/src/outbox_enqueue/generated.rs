use std::collections::BTreeMap;

use ga_domain::sha256_hex;
use serde_json::{Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use uuid::Uuid;

use super::{QueueIntent, QueueIntentError, QueuedIntent, enqueue};
use crate::{check_readiness, conversation_memory::MemoryFence, replay::REPLAY_MUTATION_LOCK};

/// Queue a generated answer only while its input revision and memory scope remain current.
pub async fn queue_generated_outbox_intent(
    txn: &mut Transaction<'_, Postgres>,
    request: QueueIntent<'_>,
    fence: &MemoryFence,
) -> Result<Option<QueuedIntent>, QueueIntentError> {
    let mut change = txn.begin().await?;
    match queue_current(&mut change, request, fence).await {
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

async fn queue_current(
    txn: &mut Transaction<'_, Postgres>,
    request: QueueIntent<'_>,
    fence: &MemoryFence,
) -> Result<Option<QueuedIntent>, QueueIntentError> {
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
    let owner = request.intent.owner_id();
    let conversation = request.intent.conversation_id();
    let operation = request.operation_id;
    let Some(inbound_id) = request.inbound_id else {
        return Err(QueueIntentError::ForeignContext);
    };
    let (epoch, shared): (Uuid, bool) = sqlx::query_as(
        "SELECT memory_epoch,share_owner_memory FROM conversations
         WHERE id=$1 AND owner_id=$2 AND channel=$3 AND channel_instance_id=$4",
    )
    .bind(conversation)
    .bind(owner)
    .bind(request.intent.channel().channel())
    .bind(request.intent.channel().instance_id())
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(QueueIntentError::ForeignContext)?;
    if epoch != fence.target_epoch {
        return Ok(None);
    }
    let sources = if shared {
        let rows: Vec<(Uuid, Uuid)> = sqlx::query_as(
            "SELECT id,memory_epoch FROM conversations WHERE owner_id=$1 AND id<>$2 AND share_owner_memory",
        ).bind(owner).bind(conversation).fetch_all(&mut **txn).await?;
        rows.into_iter().collect::<BTreeMap<_, _>>()
    } else {
        BTreeMap::new()
    };
    if sources != fence.source_epochs {
        return Ok(None);
    }
    let (revision, latest): (i32, i32) = sqlx::query_as(
        "SELECT revision,(SELECT max(revision) FROM inbound_messages
            WHERE owner_id=$2 AND conversation_id=$3 AND operation_id=$4)
         FROM inbound_messages WHERE id=$1 AND owner_id=$2 AND conversation_id=$3
                  AND operation_id=$4 AND legacy_telegram_update_id IS NULL",
    )
    .bind(inbound_id)
    .bind(owner)
    .bind(conversation)
    .bind(operation)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(QueueIntentError::ForeignContext)?;
    if revision != latest {
        return Ok(None);
    }

    let (document, key) = fence_record(fence, operation, revision);
    let receipt = enqueue(
        txn,
        QueueIntent {
            dedup_key: Some(&key),
            ..request
        },
    )
    .await?;
    if receipt.created {
        sqlx::query("UPDATE outbox_messages SET memory_fence=$2::jsonb WHERE id=$1")
            .bind(receipt.outbox_id)
            .bind(document.to_string())
            .execute(&mut **txn)
            .await?;
    } else {
        let matches: bool = sqlx::query_scalar(
            "SELECT memory_fence IS NOT DISTINCT FROM $2::jsonb FROM outbox_messages WHERE id=$1",
        )
        .bind(receipt.outbox_id)
        .bind(document.to_string())
        .fetch_one(&mut **txn)
        .await?;
        if !matches {
            return Err(QueueIntentError::ForeignContext);
        }
    }
    Ok(Some(receipt))
}

fn fence_record(fence: &MemoryFence, operation: Uuid, revision: i32) -> (Value, String) {
    let sources: BTreeMap<_, _> = fence
        .source_epochs
        .iter()
        .map(|(id, epoch)| (id.to_string(), epoch.to_string()))
        .collect();
    // Python hashes sorted JSON with its standard spacing. UUIDs need no escaping.
    let entries: Vec<_> = sources
        .iter()
        .map(|(id, epoch)| format!(r#""{id}": "{epoch}""#))
        .collect();
    let encoded = format!(
        r#"{{"source_epochs": {{{}}}, "target_epoch": "{}"}}"#,
        entries.join(", "),
        fence.target_epoch
    );
    let digest = sha256_hex(encoded.as_bytes());
    let key = format!(
        "operation:{operation}:generation:{}:revision:{revision}:reply",
        &digest[..16]
    );
    (
        json!({"source_epochs":sources,"target_epoch":fence.target_epoch.to_string()}),
        key,
    )
}
