use std::time::Duration;

use ga_domain::{
    channel::{InboundEnvelope, InboundKind, OutboundIntent},
    timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use super::{ConversationBinding, TelegramDiaryCommitError, TelegramInboundLease, prompt};
use crate::{
    Database,
    diary::{UndoDiaryOperation, cancel_in, undo::try_undo_and_reconcile},
    jobs::telegram_inbound::{
        TelegramInboundState,
        diary::{UNDO_UNAVAILABLE_REPLY, complete_reply_as, queue_reply},
    },
    outbox_enqueue::QueuedIntent,
};

#[derive(Clone, Copy)]
pub(super) enum Command {
    Cancel,
    Undo,
}

impl Command {
    pub(super) fn from_envelope(envelope: &InboundEnvelope) -> Option<Self> {
        if !matches!(envelope.kind(), InboundKind::Text | InboundKind::Voice) {
            return None;
        }
        match envelope.text()?.trim() {
            "/cancel" => Some(Self::Cancel),
            "/undo" => Some(Self::Undo),
            _ => None,
        }
    }
}

impl Database {
    pub(super) async fn apply_telegram_diary_command(
        &self,
        lease: &TelegramInboundLease,
        envelope: &InboundEnvelope,
        command: Command,
        configured_timezone: &str,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        if envelope.channel().channel() != "telegram" || envelope.message_id() != lease.message_id {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, envelope).await?;
            let in_order: bool = sqlx::query_scalar(concat!(
                "SELECT ",
                include_str!("../no_prior_diary.sql"),
                " FROM jobs j JOIN inbound_messages i ON i.id=$1 WHERE j.id=$2",
            ))
            .bind(lease.message_id())
            .bind(lease.job_identity().0)
            .fetch_one(&mut *txn)
            .await?;
            if !in_order {
                return Err(TelegramDiaryCommitError::Outbox(crate::outbox_enqueue::QueueIntentError::Busy));
            }
            let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn).await?;
            let now = StoredInstant::try_from(now).map_err(|_| TelegramDiaryCommitError::Database)?;
            let prompt = prompt::inspect(&mut txn, binding, lease, envelope, now).await?;
            let (message, state) = if let Some(notice) = prompt.notice {
                (notice.into(), TelegramInboundState::Processed)
            } else {
                match (command, prompt.draft) {
                    (Command::Cancel, Some(draft)) => {
                        (cancel_draft(&mut txn, draft).await?.into(), TelegramInboundState::Processed)
                    }
                    (Command::Cancel, None) => {
                        cancel_in(&mut txn, binding).await.map_err(TelegramDiaryCommitError::Clarification)?;
                        ("Уточнение отменено. Можно добавить новую запись.".into(), TelegramInboundState::Processed)
                    }
                    (Command::Undo, _) => undo(&mut txn, binding, envelope.sender_ref(), configured_timezone).await?,
                }
            };
            let intent = OutboundIntent::normalize(json!({
                "owner_id":binding.owner_id.to_string(),
                "conversation_id":binding.conversation_id.to_string(),
                "channel_instance":{"channel":"telegram","instance_id":envelope.channel().instance_id()},
                "blocks":[{"text":message}],
            })).map_err(|_| TelegramDiaryCommitError::Reply)?;
            let queued = queue_reply(&mut txn, lease, envelope, intent, None).await?;
            complete_reply_as(&mut txn, lease, state).await?;
            txn.commit().await?;
            Ok(queued)
        }).await.map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

async fn cancel_draft(
    txn: &mut Transaction<'_, Postgres>,
    draft: prompt::SetupDraft,
) -> Result<&'static str, sqlx::Error> {
    if draft.predates_step {
        return Ok(if draft.russian {
            "Сообщение отправлено до текущего шага. Откройте актуальное меню."
        } else {
            "This message predates the current step. Open the current menu."
        });
    }
    sqlx::query("DELETE FROM app_state WHERE key=$1")
        .bind(draft.key)
        .execute(&mut **txn)
        .await?;
    Ok(if draft.russian {
        "Черновик удалён."
    } else {
        "Draft discarded."
    })
}

async fn undo(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    sender: &str,
    configured_timezone: &str,
) -> Result<(String, TelegramInboundState), TelegramDiaryCommitError> {
    let actor = format!("telegram:{sender}");
    let Some(undone) = try_undo_and_reconcile(
        txn,
        &UndoDiaryOperation {
            owner_id: binding.owner_id,
            authenticated_actor: &actor,
        },
        configured_timezone,
    )
    .await
    .map_err(TelegramDiaryCommitError::Diary)?
    else {
        return Ok((UNDO_UNAVAILABLE_REPLY.into(), TelegramInboundState::Invalid));
    };
    cancel_in(txn, binding)
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    let message = if undone.count > 1 {
        format!("Последняя операция отменена: записей {}.", undone.count)
    } else {
        "Последнее изменение отменено.".into()
    };
    Ok((message, TelegramInboundState::Processed))
}
