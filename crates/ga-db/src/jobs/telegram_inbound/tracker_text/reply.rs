use super::super::{TelegramDiaryCommitError, TelegramInboundLease, tracker_form};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{
        ClarificationClock, actions::TrackerTextOperation, cancel_in, pending_clarification_in,
        record_history_selection,
    },
    health_analysis::owner_timezone,
};
use ga_domain::{
    channel::{InboundEnvelope, InboundKind},
    event_payload::python_whitespace,
    event_schema::chat_form::{ChatFormError, begin_chat_form},
    timestamp::{OffsetTimestamp, StoredInstant},
    tracker_nl::TrackerTextRequest,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

pub(super) async fn legacy_pending(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
) -> Result<Option<Value>, TelegramDiaryCommitError> {
    if super::super::analysis_reply::is_analytic_reply(txn, envelope).await? {
        return Ok(None);
    }
    let binding = binding(envelope);
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let clock = ClarificationClock {
        message_at: StoredInstant::try_from(
            envelope.occurred_at().unwrap_or(envelope.received_at()),
        )
        .map_err(|_| TelegramDiaryCommitError::Database)?,
        conversation_now: StoredInstant::try_from(at)
            .map_err(|_| TelegramDiaryCommitError::Database)?,
        use_message_time: false,
    };
    let (_, mut pending) = pending_clarification_in(txn, binding, clock)
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    if pending.is_none() {
        (_, pending) = pending_clarification_in(
            txn,
            binding,
            ClarificationClock {
                use_message_time: true,
                ..clock
            },
        )
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    }
    Ok(pending.filter(|pending| {
        pending["definition_version_id"].is_string()
            && !pending["chat_form"].is_object()
            && !pending["chat_close"].is_object()
    }))
}

pub(in crate::jobs::telegram_inbound) async fn fallback_in(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    timezone: &str,
    locale: &str,
) -> Result<bool, TelegramDiaryCommitError> {
    let text = envelope
        .text()
        .unwrap_or("")
        .trim_matches(python_whitespace);
    if text.is_empty() || text.starts_with('/') {
        return Ok(false);
    }
    let Some(pending) = legacy_pending(txn, envelope).await? else {
        return Ok(false);
    };
    super::super::local_text::verify_order(txn, lease).await?;
    let version = selected_version(&pending)?;
    let result = if !tracker_form::allowed(txn, envelope, version, false).await? {
        json!({"channel_denied":true})
    } else if let Ok(request) = request(envelope, version) {
        let actor = format!("telegram:{}", envelope.sender_ref());
        let scopes = [
            ga_domain::access::Scope::ReadDiary,
            ga_domain::access::Scope::WriteDiary,
        ]
        .into_iter()
        .collect();
        match Database::tracker_text_operation_in(txn, &request, &actor, scopes)
            .await
            .map_err(|_| TelegramDiaryCommitError::Database)?
        {
            TrackerTextOperation::Busy => {
                return Err(TelegramDiaryCommitError::Outbox(
                    crate::outbox_enqueue::QueueIntentError::Busy,
                ));
            }
            TrackerTextOperation::Receipt(result) => result,
            TrackerTextOperation::Pending => {
                let active: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM event_definitions d JOIN event_definition_versions v
                     ON v.definition_id=d.id AND v.version=d.current_version JOIN tracker_configs t ON t.definition_id=d.id
                     WHERE d.owner_id=$1 AND d.namespace='user' AND d.status='active' AND v.id=$2)",
                ).bind(envelope.owner_id()).bind(version).fetch_one(&mut **txn).await?;
                if active {
                    let timezone = owner_timezone(txn, timezone)
                        .await
                        .map_err(|_| TelegramDiaryCommitError::Database)?;
                    let at = StoredInstant::try_from(
                        envelope.occurred_at().unwrap_or(envelope.received_at()),
                    )
                    .map_err(|_| TelegramDiaryCommitError::Database)?;
                    Database::tracker_text_clock_in(txn, &request, &actor, at, &timezone)
                        .await
                        .map_err(|_| TelegramDiaryCommitError::Database)?;
                    json!({"intent":"deterministic_form"})
                } else {
                    json!({"invalid":true})
                }
            }
        }
    } else {
        json!({"invalid":true})
    };
    finish_in(
        txn,
        lease,
        envelope,
        pending,
        &result,
        ReplyPreferences {
            timezone,
            locale,
            safety_unavailable: true,
        },
    )
    .await?;
    Ok(true)
}

pub(in crate::jobs::telegram_inbound) async fn control_in(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    timezone: &str,
    locale: &str,
    transcribe_voice: bool,
) -> Result<bool, TelegramDiaryCommitError> {
    if legacy_pending(txn, envelope).await?.is_none() {
        return Ok(false);
    }
    let text = envelope
        .text()
        .unwrap_or("")
        .trim_matches(python_whitespace);
    let reply = if text.is_empty() {
        "Пришлите текст или голосовое сообщение."
    } else if text.starts_with('/') {
        "Неизвестная команда. Доступные команды: /help."
    } else if envelope.kind() == InboundKind::Voice && !transcribe_voice && !lease.has_voice_text()
    {
        return fallback_in(txn, lease, envelope, timezone, locale).await;
    } else {
        return Ok(false);
    };
    tracker_form::write_reply(txn, lease, envelope, reply, None).await?;
    Ok(true)
}

pub(super) struct ReplyPreferences<'a> {
    pub timezone: &'a str,
    pub locale: &'a str,
    pub safety_unavailable: bool,
}

pub(super) async fn finish_in(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    mut pending: Value,
    result: &Value,
    preferences: ReplyPreferences<'_>,
) -> Result<(), TelegramDiaryCommitError> {
    if result["invalid"] == true {
        return tracker_form::write_reply(
            txn,
            lease,
            envelope,
            super::super::diary::UNDO_UNAVAILABLE_REPLY,
            None,
        )
        .await;
    }
    let ReplyPreferences {
        timezone,
        locale,
        safety_unavailable,
    } = preferences;
    let version = selected_version(&pending)?;
    let mut keep = true;
    let mut facts = false;
    let channel_allowed = tracker_form::allowed(txn, envelope, version, false).await?;
    let mut invalid = result["invalid"] == true;
    let reply = if !channel_allowed {
        keep = false;
        "Доступ к трекеру изменился. Откройте актуальное меню.".to_owned()
    } else if result["written"] == true {
        keep = false;
        "Запись сохранена.".to_owned()
    } else if result["intent"] == "deterministic_form" {
        let form = Database::tracker_generated_form_in(
            txn,
            envelope.owner_id(),
            &format!("create:{version}"),
            locale,
        )
        .await;
        match form {
            Ok(form) => {
                let timezone = owner_timezone(txn, timezone)
                    .await
                    .map_err(|_| TelegramDiaryCommitError::Database)?;
                match begin_chat_form(&form, &timezone, locale) {
                    Ok(start) => {
                        pending["chat_form"] = start.state;
                        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                            .fetch_one(&mut **txn)
                            .await?;
                        pending["created_at"] = json!(OffsetTimestamp(now).to_string());
                        pending["prompt_advanced_at"] = json!(OffsetTimestamp(now).to_string());
                        pending["prompt_order"] = tracker_form::order(txn, lease, envelope).await?;
                        start.reply
                    }
                    Err(ChatFormError::Answer(reply)) => {
                        keep = false;
                        reply
                    }
                    Err(_) => return Err(TelegramDiaryCommitError::Database),
                }
            }
            Err(_) => {
                invalid = true;
                super::super::diary::UNDO_UNAVAILABLE_REPLY.to_owned()
            }
        }
    } else {
        facts = tracker_form::allowed(txn, envelope, version, true).await?;
        if facts {
            result["clarification"]
                .as_str()
                .filter(|value| !value.is_empty())
                .unwrap_or("Уточните значения для записи.")
                .to_owned()
        } else {
            "Уточните значения для записи.".to_owned()
        }
    };
    if keep {
        record_history_selection(txn, binding(envelope), pending)
            .await
            .map_err(TelegramDiaryCommitError::Clarification)?;
    } else {
        cancel_in(txn, binding(envelope))
            .await
            .map_err(TelegramDiaryCommitError::Clarification)?;
    }
    let reply = if safety_unavailable && !invalid {
        tracker_form::with_safety(&reply, locale)
    } else {
        reply
    };
    tracker_form::write_reply(
        txn,
        lease,
        envelope,
        &reply,
        (channel_allowed && !invalid).then_some((version, facts)),
    )
    .await
}

pub(super) fn selected_version(pending: &Value) -> Result<Uuid, TelegramDiaryCommitError> {
    pending["definition_version_id"]
        .as_str()
        .and_then(|id| id.parse().ok())
        .ok_or(TelegramDiaryCommitError::Database)
}

pub(super) fn request(
    envelope: &InboundEnvelope,
    version: Uuid,
) -> Result<TrackerTextRequest, TelegramDiaryCommitError> {
    TrackerTextRequest::from_value(&json!({"text":envelope.text(),
        "operation_id":format!("telegram:{}",envelope.external_event_id()),"selected_definition_version_id":version.to_string()}))
        .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)
}

fn binding(envelope: &InboundEnvelope) -> ConversationBinding {
    ConversationBinding {
        owner_id: envelope.owner_id(),
        conversation_id: envelope.conversation_id(),
    }
}
