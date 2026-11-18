use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{ConversationBinding, MemoryError, MemoryFence, shared_memory_epochs};
use crate::{
    channel_ingress::telegram_poll::selection, personal_goals, telegram_binding::confirmed_pairing,
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

pub(crate) async fn channel_in(
    txn: &mut Transaction<'_, Postgres>,
    context: AnalysisReadContext<'_>,
) -> Result<Option<String>, MemoryError> {
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
    let (goals, _) = personal_goals::load(txn, context.binding.owner_id)
        .await
        .map_err(|_| MemoryError::InvalidState)?;
    if goals.revision != context.goals_revision {
        return Ok(None);
    }
    if channel == "telegram" {
        let Ok(chat) = sender.parse::<i64>() else {
            return Ok(None);
        };
        if external
            .as_ref()
            .is_some_and(|external| external != &sender)
            || !selection(txn, &instance).await.unwrap_or(false)
            || confirmed_pairing(txn, &instance, chat).await.ok() != Some(context.binding.owner_id)
        {
            return Ok(None);
        }
    }
    Ok(Some(instance))
}
