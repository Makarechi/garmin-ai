use std::time::Duration;

use ga_domain::{
    channel::{InboundEnvelope, OutboundIntent},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{
    TelegramDiaryCommitError, TelegramInboundLease, TelegramModelContext,
    diary::{complete_reply, queue_reply, report, text_envelope},
    model_publication::{CurrentTelegramModelContext, queue, verify},
};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{DiaryWriteError, UndoDiaryOperation, cancel_in, undo::undo_and_reconcile},
    outbox_enqueue::QueuedIntent,
};

pub enum ModelContinuation {
    Undo { offered_audit: Option<i64> },
    Acknowledge { question_id: Uuid },
}

pub enum ModelContinuationReply {
    Undone(usize),
    ContextUnknown,
    EpisodeOpen,
    EpisodeChanged,
}

impl Database {
    /// Save only an offered acknowledgement or the exact current undo group.
    pub async fn apply_telegram_model_continuation(
        &self,
        lease: &TelegramInboundLease,
        expected: TelegramModelContext<'_>,
        work: ModelContinuation,
        reply: impl FnOnce(&InboundEnvelope, ModelContinuationReply) -> Option<OutboundIntent>,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        let envelope = text_envelope(lease)?;
        let (text, message_at) = report(&envelope)?;
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            let context = verify(&mut txn, lease, &envelope, expected).await?;
            if context.selection_unavailable {
                return Err(TelegramDiaryCommitError::ModelContextChanged);
            }
            let reply_kind = match work {
                ModelContinuation::Undo { offered_audit } => {
                    if latest_undo_anchor(&mut txn, envelope.sender_ref()).await? != offered_audit {
                        return Err(TelegramDiaryCommitError::ModelContextChanged);
                    }
                    let undone = undo_and_reconcile(
                        &mut txn,
                        &UndoDiaryOperation {
                            owner_id: binding.owner_id,
                            authenticated_actor: &format!("telegram:{}", envelope.sender_ref()),
                        },
                        expected.configured_timezone,
                    )
                    .await
                    .map_err(TelegramDiaryCommitError::Diary)?;
                    ModelContinuationReply::Undone(undone.count)
                }
                ModelContinuation::Acknowledge { question_id } => {
                    acknowledge(&mut txn, &context, question_id, text, message_at).await?
                }
            };
            if !matches!(reply_kind, ModelContinuationReply::EpisodeChanged) {
                cancel_in(&mut txn, binding)
                    .await
                    .map_err(TelegramDiaryCommitError::Clarification)?;
            }
            let intent = reply(&envelope, reply_kind).ok_or(TelegramDiaryCommitError::Reply)?;
            let queued = queue(&mut txn, lease, context, intent).await?;
            complete_reply(&mut txn, lease).await?;
            txn.commit().await?;
            Ok(queued)
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }

    /// Finish a screened message with fixed local wording without making diary changes.
    pub async fn finish_telegram_model_notice(
        &self,
        lease: &TelegramInboundLease,
        message: &str,
    ) -> Result<(), TelegramDiaryCommitError> {
        let envelope = text_envelope(lease)?;
        let intent = OutboundIntent::normalize(json!({
            "owner_id":envelope.owner_id().to_string(),
            "conversation_id":envelope.conversation_id().to_string(),
            "channel_instance":{"channel":"telegram","instance_id":envelope.channel().instance_id()},
            "blocks":[{"text":message}],
        })).map_err(|_| TelegramDiaryCommitError::Reply)?;
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            queue_reply(&mut txn, lease, &envelope, intent, None).await?;
            complete_reply(&mut txn, lease).await?;
            txn.commit().await?;
            Ok(())
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

pub(super) async fn latest_undo_anchor(
    txn: &mut Transaction<'_, Postgres>,
    sender: &str,
) -> Result<Option<i64>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT id FROM audit_log WHERE actor=$1 AND action<>'undo' ORDER BY id DESC LIMIT 1",
    )
    .bind(format!("telegram:{sender}"))
    .fetch_optional(&mut **txn)
    .await
}

async fn acknowledge(
    txn: &mut Transaction<'_, Postgres>,
    context: &CurrentTelegramModelContext,
    id: Uuid,
    text: &str,
    at: StoredInstant,
) -> Result<ModelContinuationReply, TelegramDiaryCommitError> {
    let offered = context
        .questions
        .iter()
        .find(|question| question["id"].as_str().and_then(|id| id.parse().ok()) == Some(id))
        .ok_or(TelegramDiaryCommitError::ExistingContextRequired)?;
    let (kind, event): (String, Option<Uuid>) = sqlx::query_as(
        "SELECT kind,event_id FROM pending_questions WHERE id=$1 AND status IN ('sent','uncertain','acknowledged')
          AND expires_at>clock_timestamp() FOR UPDATE",
    ).bind(id).fetch_optional(&mut **txn).await?.ok_or(TelegramDiaryCommitError::ModelContextChanged)?;
    if offered["kind"] != kind {
        return Err(TelegramDiaryCommitError::ModelContextChanged);
    }
    let mut evidence =
        json!({"answer_text":text,"answered_at":at.to_string().replace('Z', "+00:00")});
    let (status, result) = match kind.as_str() {
        "context" => {
            evidence["answer_kind"] = Value::String("unknown".into());
            ("acknowledged", ModelContinuationReply::ContextUnknown)
        }
        "migraine" => {
            let state = episode_status(txn, event, at).await?;
            match state {
                Episode::Open => ("acknowledged", ModelContinuationReply::EpisodeOpen),
                Episode::Ended => ("answered", ModelContinuationReply::EpisodeChanged),
                Episode::Unavailable => ("cancelled", ModelContinuationReply::EpisodeChanged),
            }
        }
        _ => return Err(TelegramDiaryCommitError::ExistingContextRequired),
    };
    sqlx::query(
        "UPDATE pending_questions SET status=$2,evidence=CASE WHEN $2='acknowledged'
                   THEN evidence || $3::jsonb ELSE evidence END WHERE id=$1",
    )
    .bind(id)
    .bind(status)
    .bind(evidence.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(result)
}

enum Episode {
    Open,
    Ended,
    Unavailable,
}

async fn episode_status(
    txn: &mut Transaction<'_, Postgres>,
    event: Option<Uuid>,
    at: StoredInstant,
) -> Result<Episode, TelegramDiaryCommitError> {
    let Some(event) = event else {
        return Ok(Episode::Unavailable);
    };
    let row = crate::diary::load_event(txn, event, false).await;
    let snapshot = match row {
        Ok(row) => row.snapshot,
        Err(DiaryWriteError::EventMissing) => return Ok(Episode::Unavailable),
        Err(error) => return Err(TelegramDiaryCommitError::Diary(error)),
    };
    if snapshot["deleted"] == true
        || snapshot["kind"] != "migraine"
        || snapshot["status"] != "confirmed"
    {
        return Ok(Episode::Unavailable);
    }
    if StoredInstant::from_legacy_input(&snapshot["end"]).is_some_and(|end| end <= at) {
        return Ok(Episode::Ended);
    }
    if StoredInstant::from_legacy_input(&snapshot["start"]).is_some_and(|start| start <= at) {
        return Ok(Episode::Open);
    }
    Ok(Episode::Unavailable)
}
