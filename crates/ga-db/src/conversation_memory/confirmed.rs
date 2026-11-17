use std::collections::BTreeMap;

use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    Conversation, ConversationBinding, DEADLINE, MemoryError, MemoryFence, RECENT_MICROS,
    bounded_turns, conversation, persist_turns, recent_turns,
};
use crate::Database;

pub struct ConfirmedAnalysis<'a> {
    pub binding: ConversationBinding,
    pub operation_id: Uuid,
    pub outbox_id: Uuid,
    pub fence: &'a MemoryFence,
    pub question: &'a str,
    pub answer: &'a str,
    pub now: StoredInstant,
}

impl Database {
    /// Retain the exact analysis text only after the matching reply was delivered.
    pub async fn remember_confirmed_analysis(
        &self,
        analysis: ConfirmedAnalysis<'_>,
    ) -> Result<bool, MemoryError> {
        tokio::time::timeout(DEADLINE, async {
            let mut txn = self.begin_memory().await?;
            let recorded = remember(&mut txn, analysis).await?;
            txn.commit().await?;
            Ok(recorded)
        })
        .await
        .map_err(|_| MemoryError::Database)?
    }
}

type DeliveredReply = (
    String,
    Option<String>,
    Option<String>,
    OffsetDateTime,
    i32,
    i32,
);

async fn remember(
    txn: &mut Transaction<'_, Postgres>,
    analysis: ConfirmedAnalysis<'_>,
) -> Result<bool, MemoryError> {
    let target = conversation(txn, analysis.binding).await?;
    if target.epoch != analysis.fence.target_epoch {
        return Ok(false);
    }
    let mut source_epochs = current_source_epochs(txn, analysis.binding, &target).await?;
    if source_epochs != analysis.fence.source_epochs {
        return Ok(false);
    }
    let (intent, pinned, question, asked_at, revision, newest) =
        delivered_reply(txn, &analysis).await?;
    if let Some(pinned) = pinned.as_deref().filter(|value| *value != "null")
        && read_fence(pinned)? != *analysis.fence
    {
        return Ok(false);
    }
    if revision != newest {
        return Ok(false);
    }
    let intent: Value = serde_json::from_str(&intent).map_err(|_| MemoryError::InvalidState)?;
    let intent = OutboundIntent::normalize(intent).map_err(|_| MemoryError::InvalidState)?;
    if intent.intent_id() != analysis.outbox_id
        || intent.conversation_id() != analysis.binding.conversation_id
        || intent.owner_id() != analysis.binding.owner_id
        || question.as_deref() != Some(analysis.question)
    {
        return Err(MemoryError::Unconfirmed);
    }
    let blocks = intent.document()["blocks"]
        .as_array()
        .ok_or(MemoryError::InvalidState)?;
    let answer: Vec<_> = blocks
        .iter()
        .map(|block| block["text"].as_str().ok_or(MemoryError::InvalidState))
        .collect::<Result<_, _>>()?;
    if answer.join("\n") != analysis.answer {
        return Err(MemoryError::Unconfirmed);
    }
    let asked_at = StoredInstant::try_from(asked_at).map_err(|_| MemoryError::InvalidState)?;
    let elapsed = analysis
        .now
        .unix_microseconds()
        .saturating_sub(asked_at.unix_microseconds());
    if !(0..=RECENT_MICROS).contains(&elapsed) {
        return Ok(false);
    }
    if target.shared {
        source_epochs.insert(target.id, target.epoch);
    }
    let recent = recent_turns(&target.turns, target.shared, &source_epochs, analysis.now)?;
    let operation = analysis.operation_id.to_string();
    if recent.iter().any(|turn| {
        turn["operation_id"].as_str() == Some(&operation)
            && turn["revision"].as_i64().unwrap_or(1) >= i64::from(revision)
    }) {
        return Ok(false);
    }
    let mut retained: Vec<Value> = recent
        .into_iter()
        .filter(|turn| turn["operation_id"].as_str() != Some(&operation))
        .cloned()
        .collect();
    let sources: BTreeMap<_, _> = analysis
        .fence
        .source_epochs
        .iter()
        .map(|(source, epoch)| (source.to_string(), epoch.to_string()))
        .collect();
    retained.push(json!({
        "operation_id": operation,
        "revision": revision,
        "asked_at": asked_at.to_string(),
        "question": analysis.question.chars().take(1000).collect::<String>(),
        "answer": analysis.answer.chars().take(1500).collect::<String>(),
        "question_truncated": analysis.question.chars().count() > 1000,
        "answer_truncated": analysis.answer.chars().count() > 1500,
        "source_epochs": sources,
    }));
    let retained = bounded_turns(retained)?;
    persist_turns(txn, analysis.binding, &retained).await?;
    Ok(true)
}

async fn current_source_epochs(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    target: &Conversation,
) -> Result<BTreeMap<Uuid, Uuid>, MemoryError> {
    if !target.shared {
        return Ok(BTreeMap::new());
    }
    let rows: Vec<(Uuid, Uuid)> = sqlx::query_as(
        "SELECT id,memory_epoch FROM conversations WHERE owner_id=$1 AND id<>$2 AND share_owner_memory",
    ).bind(binding.owner_id).bind(binding.conversation_id).fetch_all(&mut **txn).await?;
    Ok(rows.into_iter().collect())
}

async fn delivered_reply(
    txn: &mut Transaction<'_, Postgres>,
    analysis: &ConfirmedAnalysis<'_>,
) -> Result<DeliveredReply, MemoryError> {
    sqlx::query_as(
        "SELECT o.intent::text,o.memory_fence::text,i.normalized_text,i.received_at,i.revision,
               (SELECT max(revision) FROM inbound_messages
                WHERE owner_id=$2 AND conversation_id=$3 AND operation_id=$4)
           FROM outbox_messages o JOIN inbound_messages i ON i.id=o.inbound_message_id
         WHERE o.id=$1 AND o.owner_id=$2 AND o.conversation_id=$3 AND o.operation_id=$4
           AND i.owner_id=$2 AND i.conversation_id=$3 AND i.operation_id=$4
           AND i.channel=o.intent->'channel_instance'->>'channel'
           AND i.channel_instance_id=o.intent->'channel_instance'->>'instance_id'
           AND o.state IN ('delivered','read')
           AND EXISTS (SELECT 1 FROM message_delivery_receipts r WHERE r.outbox_message_id=o.id
                                                          AND r.state IN ('delivered','read'))",
    )
    .bind(analysis.outbox_id)
    .bind(analysis.binding.owner_id)
    .bind(analysis.binding.conversation_id)
    .bind(analysis.operation_id)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(MemoryError::Unconfirmed)
}

fn read_fence(raw: &str) -> Result<MemoryFence, MemoryError> {
    let value: Value = serde_json::from_str(raw).map_err(|_| MemoryError::InvalidState)?;
    let target_epoch = value["target_epoch"]
        .as_str()
        .and_then(|value| value.parse().ok())
        .ok_or(MemoryError::InvalidState)?;
    let sources = value["source_epochs"]
        .as_object()
        .ok_or(MemoryError::InvalidState)?;
    let source_epochs = sources
        .iter()
        .map(|(source, epoch)| {
            let source = source.parse().map_err(|_| MemoryError::InvalidState)?;
            let epoch = epoch
                .as_str()
                .and_then(|epoch| epoch.parse().ok())
                .ok_or(MemoryError::InvalidState)?;
            Ok((source, epoch))
        })
        .collect::<Result<_, MemoryError>>()?;
    Ok(MemoryFence {
        target_epoch,
        source_epochs,
    })
}
