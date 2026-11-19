use std::time::Duration;

use ga_domain::{
    agent_interpretation::AgentInterpretation,
    channel::{InboundEnvelope, OutboundIntent},
    model_consent::ModelCategories,
};

use super::{
    TelegramDiaryCommitError, TelegramInboundLease, TelegramModelContext,
    diary::{complete_reply, report, text_envelope},
    model_publication::{queue, verify},
};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{
        StoredSystemEvent,
        agent_correction::{AgentCorrection, AgentCorrectionContext, AgentCorrectionError},
        cancel_in,
    },
    outbox_enqueue::QueuedIntent,
};

impl Database {
    /// Correct the exact offered system event and keep its audit and reply atomic.
    pub async fn apply_telegram_model_correction(
        &self,
        lease: &TelegramInboundLease,
        command: &AgentInterpretation,
        expected: TelegramModelContext<'_>,
        reply: impl FnOnce(&InboundEnvelope, &[StoredSystemEvent]) -> Option<OutboundIntent>,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        let correction = AgentCorrection::parse(command)?;
        let envelope = text_envelope(lease)?;
        let (text, received_at) = report(&envelope)?;
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            let context = verify(
                &mut txn,
                lease,
                &envelope,
                expected,
                ModelCategories::HEALTH_AND_DIARY,
            )
            .await?;
            if context.selection_unavailable {
                return Err(TelegramDiaryCommitError::ModelContextChanged);
            }
            let edited = correction
                .apply(
                    &mut txn,
                    AgentCorrectionContext {
                        owner_id: envelope.owner_id(),
                        actor: &format!("telegram:{}", envelope.sender_ref()),
                        operation_id: lease.operation_id(),
                        text,
                        received_at,
                        offered: &context.offered,
                        timezone: expected.timezone,
                    },
                )
                .await?;
            cancel_in(
                &mut txn,
                ConversationBinding {
                    owner_id: envelope.owner_id(),
                    conversation_id: envelope.conversation_id(),
                },
            )
            .await
            .map_err(TelegramDiaryCommitError::Clarification)?;
            let intent = reply(&envelope, &[edited]).ok_or(TelegramDiaryCommitError::Reply)?;
            let queued = queue(&mut txn, lease, context, intent).await?;
            complete_reply(&mut txn, lease).await?;
            txn.commit().await?;
            Ok(queued)
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

impl From<AgentCorrectionError> for TelegramDiaryCommitError {
    fn from(error: AgentCorrectionError) -> Self {
        match error {
            AgentCorrectionError::ClarificationRequired => Self::ClarificationRequired,
            AgentCorrectionError::ExistingContextRequired => Self::ExistingContextRequired,
            AgentCorrectionError::ModelContextChanged => Self::ModelContextChanged,
            AgentCorrectionError::TimeClarification(error) => Self::TimeClarification(error),
            AgentCorrectionError::Diary(error) => Self::Diary(error),
            AgentCorrectionError::Database => Self::Database,
        }
    }
}
