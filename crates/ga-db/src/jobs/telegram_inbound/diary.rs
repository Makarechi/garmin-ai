use std::{error::Error, fmt, time::Duration};

use ga_domain::{
    agent_interpretation::{AgentIntent, AgentInterpretation, TimeClarification},
    channel::{InboundEnvelope, InboundKind, OutboundIntent},
    event_input::EventSource,
    timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use super::{
    KIND, TelegramInboundLease, TelegramInboundState, complete, superseded, try_ingress_lock,
};
use crate::{
    Database,
    channel_ingress::telegram_poll::selection,
    conversation_memory::ConversationBinding,
    diary::{
        ClarificationClock, ClarificationError, ClarificationReply, CreateAgentLogBatch,
        DiaryWriteError, StoredSystemEvent, save_clarification_in,
    },
    jobs::{JobStoreError, begin},
    outbox_enqueue::{QueueIntent, QueueIntentError, QueuedIntent, queue_outbox_intent},
    telegram_binding::confirmed_pairing,
};

#[derive(Debug)]
pub enum TelegramDiaryCommitError {
    Job(JobStoreError),
    BindingChanged,
    UnsupportedMessage,
    ClarificationRequired,
    TimeClarification(TimeClarification),
    ExistingContextRequired,
    MedicationEvidenceRequired,
    Reply,
    Diary(DiaryWriteError),
    Clarification(ClarificationError),
    Outbox(QueueIntentError),
    Database,
}

impl fmt::Display for TelegramDiaryCommitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Job(error) => error.fmt(f),
            Self::Diary(error) => error.fmt(f),
            Self::Clarification(error) => error.fmt(f),
            Self::Outbox(error) => error.fmt(f),
            Self::TimeClarification(error) => error.fmt(f),
            Self::BindingChanged => f.write_str("Telegram owner or selected channel changed"),
            Self::UnsupportedMessage => {
                f.write_str("The stored message cannot be used as a text report")
            }
            Self::ClarificationRequired => f.write_str("The diary report requires clarification"),
            Self::ExistingContextRequired => {
                f.write_str("This report requires a current, explicitly offered record")
            }
            Self::MedicationEvidenceRequired => {
                f.write_str("Medication reports require explicit intake evidence")
            }
            Self::Reply => f.write_str("A diary confirmation could not be prepared"),
            Self::Database => f.write_str("Diary work could not be committed"),
        }
    }
}
impl Error for TelegramDiaryCommitError {}
impl From<sqlx::Error> for TelegramDiaryCommitError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}
impl From<JobStoreError> for TelegramDiaryCommitError {
    fn from(error: JobStoreError) -> Self {
        Self::Job(error)
    }
}

impl Database {
    /// Commit an authenticated, self-contained text report, its reply and the
    /// processing job together. The caller builds a local reply from saved rows.
    pub async fn apply_telegram_text_log(
        &self,
        lease: &TelegramInboundLease,
        command: &AgentInterpretation,
        reply: impl FnOnce(&InboundEnvelope, &[StoredSystemEvent]) -> Option<OutboundIntent>,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        if command.intent() != AgentIntent::Log || command.confidence() < 0.85 {
            return Err(TelegramDiaryCommitError::ClarificationRequired);
        }
        if command.target_event_id().is_some()
            || command.target_question_id().is_some()
            || !command.question_context_ids().is_empty()
            || command.events().iter().any(|event| {
                ["reason_event_id", "episode_id", "activity_id"]
                    .iter()
                    .any(|field| !event.payload().json()[field].is_null())
            })
        {
            return Err(TelegramDiaryCommitError::ExistingContextRequired);
        }

        if command
            .events()
            .iter()
            .any(|event| event.payload().kind() == "medication")
        {
            return Err(TelegramDiaryCommitError::MedicationEvidenceRequired);
        }

        let envelope = text_envelope(lease)?;
        let (text, reference) = report(&envelope)?;
        command
            .verify_time_evidence(text, reference, None)
            .map_err(TelegramDiaryCommitError::TimeClarification)?;

        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            if envelope.kind() == InboundKind::Edit {
                let already_written: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM audit_log WHERE operation_id=$1 AND action='create')",
                ).bind(lease.operation_id).fetch_one(&mut *txn).await?;
                if already_written {
                    return Err(TelegramDiaryCommitError::ExistingContextRequired);
                }
            }
            let operation = lease.operation_id;
            let key = format!("telegram:native:{operation}");
            let actor = format!("telegram:{}", envelope.sender_ref());
            let events = Self::create_agent_log_batch_in(&mut txn, CreateAgentLogBatch {
                owner_id: envelope.owner_id(),
                command,
                authenticated_actor: &actor,
                message_text: text,
                source: EventSource::TelegramText,
                operation_id: operation,
                idempotency_prefix: &key,
            }).await.map_err(TelegramDiaryCommitError::Diary)?;
            let intent = reply(&envelope, &events).ok_or(TelegramDiaryCommitError::Reply)?;
            let queued = queue_reply(&mut txn, lease, &envelope, intent).await?;
            txn.commit().await?;
            Ok(queued)
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }

    /// Save a clarification, enqueue its visible question, and consume the exact
    /// authenticated message together. A retry cannot append the same turn twice.
    pub async fn apply_telegram_text_clarification(
        &self,
        lease: &TelegramInboundLease,
        processing_started: StoredInstant,
        question: &str,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        let envelope = text_envelope(lease)?;
        let (text, message_at) = report(&envelope)?;
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        let intent = OutboundIntent::normalize(json!({
            "owner_id": binding.owner_id.to_string(),
            "conversation_id": binding.conversation_id.to_string(),
            "channel_instance": {"channel":"telegram","instance_id":envelope.channel().instance_id()},
            "blocks": [{"text":question}],
        }))
        .map_err(|_| TelegramDiaryCommitError::Reply)?;
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            save_clarification_in(
                &mut txn,
                binding,
                ClarificationReply {
                    clock: ClarificationClock {
                        message_at,
                        conversation_now: processing_started,
                        use_message_time: false,
                    },
                    text,
                    question,
                    selection_prompt: None,
                },
            )
            .await
            .map_err(TelegramDiaryCommitError::Clarification)?;
            let queued = queue_reply(&mut txn, lease, &envelope, intent).await?;
            txn.commit().await?;
            Ok(queued)
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }

    async fn begin_telegram_text(
        &self,
        lease: &TelegramInboundLease,
        envelope: &InboundEnvelope,
    ) -> Result<Transaction<'_, Postgres>, TelegramDiaryCommitError> {
        let mut txn = begin(&self.work).await?;
        if !try_ingress_lock(&mut txn).await? {
            return Err(JobStoreError::Database.into());
        }
        let owner = envelope.owner_id();
        let instance = envelope.channel().instance_id();
        let chat = envelope
            .sender_ref()
            .parse()
            .map_err(|_| TelegramDiaryCommitError::BindingChanged)?;
        if !selection(&mut txn, instance)
            .await
            .map_err(|_| TelegramDiaryCommitError::BindingChanged)?
            || confirmed_pairing(&mut txn, instance, chat)
                .await
                .map_err(|_| TelegramDiaryCommitError::BindingChanged)?
                != owner
        {
            return Err(TelegramDiaryCommitError::BindingChanged);
        }
        let active: bool = sqlx::query_scalar(
            "SELECT EXISTS (
               SELECT 1 FROM jobs j JOIN inbound_messages i
                 ON j.dedup_key=concat('telegram:inbound:native:',i.id)
               WHERE j.id=$1 AND j.lease_token=$2 AND j.kind=$3 AND j.status='running'
                 AND j.lease_until>clock_timestamp() AND i.id=$4 AND i.status='pending'
                 AND i.legacy_telegram_update_id IS NULL AND i.envelope=$5::jsonb
                 AND i.operation_id=$6
            )",
        )
        .bind(lease.job_id)
        .bind(lease.token)
        .bind(KIND)
        .bind(lease.message_id)
        .bind(lease.envelope_json())
        .bind(lease.operation_id)
        .fetch_one(&mut *txn)
        .await?;
        if !active || superseded(&mut txn, lease.message_id).await? {
            return Err(JobStoreError::LeaseLost.into());
        }
        Self::require_diary_owner(&mut txn, owner)
            .await
            .map_err(TelegramDiaryCommitError::Diary)?;
        Ok(txn)
    }
}

fn text_envelope(
    lease: &TelegramInboundLease,
) -> Result<InboundEnvelope, TelegramDiaryCommitError> {
    let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
        .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
    if !matches!(envelope.kind(), InboundKind::Text | InboundKind::Edit)
        || envelope.message_id() != lease.message_id
        || envelope.channel().channel() != "telegram"
    {
        return Err(TelegramDiaryCommitError::UnsupportedMessage);
    }
    Ok(envelope)
}

fn report(envelope: &InboundEnvelope) -> Result<(&str, StoredInstant), TelegramDiaryCommitError> {
    let text = envelope
        .text()
        .filter(|text| !text.trim().is_empty())
        .ok_or(TelegramDiaryCommitError::UnsupportedMessage)?;
    let reference =
        StoredInstant::try_from(envelope.occurred_at().unwrap_or(envelope.received_at()))
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
    Ok((text, reference))
}

async fn queue_reply(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    intent: OutboundIntent,
) -> Result<QueuedIntent, TelegramDiaryCommitError> {
    if intent.owner_id() != envelope.owner_id()
        || intent.conversation_id() != envelope.conversation_id()
        || intent.channel().channel() != "telegram"
        || intent.channel().instance_id() != envelope.channel().instance_id()
    {
        return Err(TelegramDiaryCommitError::Reply);
    }
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let queued = queue_outbox_intent(
        txn,
        QueueIntent {
            intent,
            operation_id: lease.operation_id,
            inbound_id: Some(lease.message_id),
            dedup_key: None,
            requirements: &[],
            now: now
                .try_into()
                .map_err(|_| TelegramDiaryCommitError::Database)?,
        },
    )
    .await
    .map_err(TelegramDiaryCommitError::Outbox)?;
    let completed_at = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    complete(txn, lease, completed_at, TelegramInboundState::Processed).await?;
    Ok(queued)
}
