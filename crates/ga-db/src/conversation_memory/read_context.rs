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
pub enum AnalysisSubject<'a> {
    Inbound(AnalysisReadContext<'a>),
    Web {
        binding: ConversationBinding,
        memory: &'a MemoryFence,
        goals_revision: u64,
    },
}

impl AnalysisSubject<'_> {
    pub(crate) fn binding(self) -> ConversationBinding {
        match self {
            Self::Inbound(context) => context.binding,
            Self::Web { binding, .. } => binding,
        }
    }
}

impl<'a> From<AnalysisReadContext<'a>> for AnalysisSubject<'a> {
    fn from(context: AnalysisReadContext<'a>) -> Self {
        Self::Inbound(context)
    }
}

#[derive(Clone, Copy)]
pub struct ConversationReadContext<'a> {
    pub binding: ConversationBinding,
    pub inbound_id: Uuid,
    pub operation_id: Uuid,
    pub memory: &'a MemoryFence,
}

pub(crate) async fn channel_in<'a>(
    txn: &mut Transaction<'_, Postgres>,
    context: impl Into<AnalysisSubject<'a>>,
) -> Result<Option<String>, MemoryError> {
    let context = context.into();
    let (channel, goals_revision) = match context {
        AnalysisSubject::Inbound(inbound) => (
            current_conversation_channel(
                txn,
                ConversationReadContext {
                    binding: inbound.binding,
                    inbound_id: inbound.inbound_id,
                    operation_id: inbound.operation_id,
                    memory: inbound.memory,
                },
            )
            .await?
            .map(|(_, instance)| instance),
            inbound.goals_revision,
        ),
        AnalysisSubject::Web {
            binding,
            memory,
            goals_revision,
        } => (
            current_web_conversation(txn, binding, memory)
                .await?
                .then(|| "web:local".to_owned()),
            goals_revision,
        ),
    };
    let Some(channel) = channel else {
        return Ok(None);
    };
    let (goals, _) = personal_goals::load(txn, context.binding().owner_id)
        .await
        .map_err(|_| MemoryError::InvalidState)?;
    Ok((goals.revision == goals_revision).then_some(channel))
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

pub(crate) async fn current_web_conversation(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    memory: &MemoryFence,
) -> Result<bool, MemoryError> {
    let row: Option<(Uuid, bool)> = sqlx::query_as(
        "SELECT c.memory_epoch,c.share_owner_memory
         FROM conversations c JOIN people p ON p.id=c.owner_id AND p.singleton
         WHERE c.id=$1 AND c.owner_id=$2 AND c.channel='web' AND c.channel_instance_id='local'",
    )
    .bind(binding.conversation_id)
    .bind(binding.owner_id)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((epoch, shared)) = row else {
        return Ok(false);
    };
    Ok(epoch == memory.target_epoch
        && shared_memory_epochs(txn, binding, shared).await? == memory.source_epochs)
}
