use std::time::Duration;

use ga_domain::{
    channel::{InboundEnvelope, InboundKind, OutboundIntent},
    timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::Postgres;
use time::OffsetDateTime;

use super::{
    TelegramDiaryCommitError, TelegramInboundLease, TelegramInboundState, complete,
    diary::queue_reply, history::prompt,
};
use crate::{
    Database, conversation_memory::ConversationBinding, health_analysis::owner_timezone,
    outbox_enqueue::QueueIntentError,
};

pub enum TelegramSafetyDecision<'a> {
    Clear,
    Urgent {
        russian: &'a str,
        english: &'a str,
        setup_metadata: SetupMetadata,
    },
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum SetupMetadata {
    None,
    Name,
    Field,
}

pub enum LocalTelegramText<'a> {
    Safety(TelegramSafetyDecision<'a>),
    ProviderUnavailable,
}

pub enum TelegramModelAdmission {
    Ready {
        timezone: String,
        locale: String,
        undo_anchor: Option<i64>,
    },
    Replied,
}

impl Database {
    /// Check the current ordered owner text and local form before sending any model input.
    pub async fn admit_telegram_model_text(
        &self,
        lease: &TelegramInboundLease,
        configured_timezone: &str,
        configured_locale: &str,
    ) -> Result<TelegramModelAdmission, TelegramDiaryCommitError> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        if envelope.channel().channel() != "telegram"
            || envelope.message_id() != lease.message_id()
            || !matches!(envelope.kind(), InboundKind::Text | InboundKind::Edit)
            || !envelope.text().is_some_and(|text| {
                let text = text.trim();
                !text.is_empty() && !text.starts_with('/')
            })
        {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            let screened: bool = sqlx::query_scalar(
                "SELECT payload->>'local_safety_checked' IS NOT DISTINCT FROM 'true'
                 FROM jobs WHERE id=$1",
            )
            .bind(lease.job_identity().0)
            .fetch_one(&mut *txn)
            .await?;
            if !screened {
                return Err(TelegramDiaryCommitError::UnsupportedMessage);
            }
            verify_order(&mut txn, lease).await?;
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            let now =
                StoredInstant::try_from(at).map_err(|_| TelegramDiaryCommitError::Database)?;
            let prompt = prompt::inspect(&mut txn, binding, lease, &envelope, now).await?;
            if let Some(notice) = prompt.notice {
                write_reply(&mut txn, lease, &envelope, at, notice).await?;
                txn.commit().await?;
                return Ok(TelegramModelAdmission::Replied);
            }
            if prompt.draft.is_some() || prompt.guided_form {
                return Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy));
            }
            let timezone = owner_timezone(&mut txn, configured_timezone)
                .await
                .map_err(|_| TelegramDiaryCommitError::Database)?;
            let locale = preferred_locale(&mut txn, binding, configured_locale).await?;
            let undo_anchor =
                super::model_reply::latest_undo_anchor(&mut txn, envelope.sender_ref()).await?;
            txn.commit().await?;
            Ok(TelegramModelAdmission::Ready {
                timezone,
                locale,
                undo_anchor,
            })
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }

    /// Screen or locally answer one precise owner message without sending it to a provider.
    pub async fn apply_local_telegram_text(
        &self,
        lease: &TelegramInboundLease,
        work: LocalTelegramText<'_>,
        configured_locale: &str,
    ) -> Result<(), TelegramDiaryCommitError> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        let has_report_text = envelope.text().is_some_and(|text| {
            let text = text.trim();
            !text.is_empty() && !text.starts_with('/')
        });
        let bare_voice = envelope.kind() == InboundKind::Voice
            && envelope.text().is_none_or(|text| text.trim().is_empty())
            && matches!(work, LocalTelegramText::ProviderUnavailable);
        if envelope.channel().channel() != "telegram"
            || envelope.message_id() != lease.message_id()
            || !matches!(
                envelope.kind(),
                InboundKind::Text | InboundKind::Edit | InboundKind::Voice
            )
            || !(has_report_text || bare_voice)
        {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn).await?;
            let now = StoredInstant::try_from(at).map_err(|_| TelegramDiaryCommitError::Database)?;
            let reply = match work {
                LocalTelegramText::Safety(TelegramSafetyDecision::Clear) => None,
                LocalTelegramText::Safety(TelegramSafetyDecision::Urgent { russian, english, setup_metadata }) => {
                    let prompt = prompt::inspect(&mut txn, binding, lease, &envelope, now).await?;
                    if prompt.draft.as_ref().is_some_and(|draft|
                        setup_metadata == SetupMetadata::Field
                            || (setup_metadata == SetupMetadata::Name && draft.awaiting_name)) {
                        None
                    } else {
                        let locale = preferred_locale(&mut txn, binding, configured_locale).await?;
                        Some(if locale.split('-').next().is_some_and(|code| code.eq_ignore_ascii_case("ru")) {
                            russian
                        } else {
                            english
                        })
                    }
                }
                LocalTelegramText::ProviderUnavailable => {
                    verify_order(&mut txn, lease).await?;
                    let prompt = prompt::inspect(&mut txn, binding, lease, &envelope, now).await?;
                    if let Some(notice) = prompt.notice {
                        Some(notice)
                    } else if prompt.draft.is_some() || prompt.pending {
                        return Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy));
                    } else if envelope.kind() == InboundKind::Voice {
                        Some("Голосовые сообщения сейчас недоступны. Показатели доступны через /today, записи — через кнопки.")
                    } else {
                        Some("Обработка свободного текста пока недоступна. Записи можно добавить кнопками, показатели посмотреть через /today.")
                    }
                }
            };
            if let Some(text) = reply {
                write_reply(&mut txn, lease, &envelope, at, text).await?;
            } else {
                let (id, token) = lease.job_identity();
                let released = sqlx::query(
                    "UPDATE jobs SET status='pending',lease_token=NULL,lease_until=NULL,attempts=attempts-1,
                       payload=jsonb_set(payload,'{local_safety_checked}','true')
                     WHERE id=$1 AND lease_token=$2 AND status='running' AND lease_until>clock_timestamp()",
                ).bind(id).bind(token).execute(&mut *txn).await?;
                if released.rows_affected() != 1 {
                    return Err(crate::jobs::JobStoreError::LeaseLost.into());
                }
            }
            txn.commit().await?;
            Ok(())
        }).await.map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

async fn preferred_locale(
    txn: &mut sqlx::Transaction<'_, Postgres>,
    binding: ConversationBinding,
    configured: &str,
) -> Result<String, sqlx::Error> {
    let stored: Option<String> = sqlx::query_scalar(
        "SELECT locale FROM people WHERE id=$1
           AND EXISTS(SELECT 1 FROM app_state WHERE key='preferences:onboarding')",
    )
    .bind(binding.owner_id)
    .fetch_optional(&mut **txn)
    .await?;
    Ok(stored.unwrap_or_else(|| configured.to_owned()))
}

async fn verify_order(
    txn: &mut sqlx::Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
) -> Result<(), TelegramDiaryCommitError> {
    let in_order: bool = sqlx::query_scalar(concat!(
        "SELECT ",
        include_str!("no_prior_diary.sql"),
        " FROM jobs j JOIN inbound_messages i ON i.id=$1 WHERE j.id=$2",
    ))
    .bind(lease.message_id())
    .bind(lease.job_identity().0)
    .fetch_one(&mut **txn)
    .await?;
    if !in_order {
        return Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy));
    }
    Ok(())
}

async fn write_reply(
    txn: &mut sqlx::Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    now: OffsetDateTime,
    text: &str,
) -> Result<(), TelegramDiaryCommitError> {
    let intent = OutboundIntent::normalize(json!({
        "owner_id":envelope.owner_id().to_string(),
        "conversation_id":envelope.conversation_id().to_string(),
        "channel_instance":{"channel":"telegram","instance_id":envelope.channel().instance_id()},
        "blocks":[{"text":text}],
    }))
    .map_err(|_| TelegramDiaryCommitError::Reply)?;
    let key = format!("inbound:{}:local-text", lease.message_id());
    queue_reply(txn, lease, envelope, intent, Some(&key)).await?;
    complete(txn, lease, now, TelegramInboundState::Processed).await?;
    Ok(())
}
