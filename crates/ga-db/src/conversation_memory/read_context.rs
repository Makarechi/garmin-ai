use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{ConversationBinding, MemoryError, MemoryFence, shared_memory_epochs};
use crate::{
    channel_ingress::telegram_poll::{TelegramPollError, selection},
    personal_goals,
    telegram_binding::{TelegramBindingError, confirmed_pairing},
};

/// The trusted question and policy revisions captured by an authenticated channel.
#[derive(Clone, Copy)]
pub struct AnalysisReadContext<'a> {
    pub binding: ConversationBinding,
    pub inbound_id: Uuid,
    pub operation_id: Uuid,
    pub memory: &'a MemoryFence,
    pub goals_revision: u64,
}

#[derive(Clone, Copy)]
pub struct ConversationReadContext<'a> {
    pub binding: ConversationBinding,
    pub inbound_id: Uuid,
    pub operation_id: Uuid,
    pub memory: &'a MemoryFence,
}

pub(crate) async fn channel_in(
    txn: &mut Transaction<'_, Postgres>,
    context: AnalysisReadContext<'_>,
) -> Result<Option<String>, MemoryError> {
    let channel = current_conversation_channel(
        txn,
        ConversationReadContext {
            binding: context.binding,
            inbound_id: context.inbound_id,
            operation_id: context.operation_id,
            memory: context.memory,
        },
    )
    .await?;
    let Some((_, instance)) = channel else {
        return Ok(None);
    };
    let (goals, _) = personal_goals::load(txn, context.binding.owner_id)
        .await
        .map_err(|_| MemoryError::InvalidState)?;
    Ok((goals.revision == context.goals_revision).then_some(instance))
}

pub(crate) async fn current_conversation_channel(
    txn: &mut Transaction<'_, Postgres>,
    context: ConversationReadContext<'_>,
) -> Result<Option<(String, String)>, MemoryError> {
    type QuestionRow = (Uuid, bool, String, String, Option<String>, String);
    let row: Option<QuestionRow> = sqlx::query_as(
        "SELECT conversation.memory_epoch,conversation.share_owner_memory,conversation.channel_instance_id,
                conversation.channel,conversation.external_conversation_id,message.sender_ref
         FROM conversations conversation
         JOIN people owner ON owner.id=conversation.owner_id AND owner.singleton
         JOIN inbound_messages message ON message.conversation_id=conversation.id
            AND message.owner_id=conversation.owner_id AND message.channel=conversation.channel
            AND message.channel_instance_id=conversation.channel_instance_id
         WHERE conversation.id=$1 AND conversation.owner_id=$2
           AND message.id=$3 AND message.operation_id=$4 AND message.legacy_telegram_update_id IS NULL
           AND message.revision=(SELECT max(latest.revision) FROM inbound_messages latest
             WHERE latest.owner_id=$2 AND latest.conversation_id=$1 AND latest.operation_id=$4)",
    )
    .bind(context.binding.conversation_id)
    .bind(context.binding.owner_id)
    .bind(context.inbound_id)
    .bind(context.operation_id)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((epoch, shared, instance, channel, external, sender)) = row else {
        return Ok(None);
    };
    if epoch != context.memory.target_epoch
        || shared_memory_epochs(txn, context.binding, shared).await? != context.memory.source_epochs
    {
        return Ok(None);
    }
    if channel == "telegram" {
        let Ok(chat) = sender.parse::<i64>() else {
            return Ok(None);
        };
        if external
            .as_ref()
            .is_some_and(|external| external != &sender)
        {
            return Ok(None);
        }
        match selection(txn, &instance).await {
            Ok(true) => {}
            Err(TelegramPollError::Busy) => return Err(MemoryError::Busy),
            _ => return Ok(None),
        }
        match confirmed_pairing(txn, &instance, chat).await {
            Ok(owner) if owner == context.binding.owner_id => {}
            Err(TelegramBindingError::Busy) => return Err(MemoryError::Busy),
            _ => return Ok(None),
        }
    }
    Ok(Some((channel, instance)))
}
