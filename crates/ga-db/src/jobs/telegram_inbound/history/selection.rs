use std::time::Duration;

use ga_domain::{
    channel::{InboundEnvelope, InboundKind, OutboundIntent},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ALL_HISTORY, HistoryPage, OPEN_MIGRAINES, Record, TelegramDiaryCommitError,
    TelegramInboundLease, TelegramInboundState, complete, labels, prompt, rows,
};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{DeleteEvent, clear_selected_record, record_history_selection},
    outbox_enqueue::{QueueIntent, QueuedIntent, queue_outbox_intent},
};

#[derive(Clone, Copy, Eq, PartialEq)]
enum Action {
    Edit,
    Close,
    Delete,
}

struct Selection {
    action: Action,
    id: Uuid,
    revision: i32,
}

impl Selection {
    fn from_envelope(envelope: &InboundEnvelope) -> Option<Self> {
        if envelope.kind() != InboundKind::Action {
            return None;
        }
        let document = envelope.document();
        let mut parts = document["action"]["action_id"].as_str()?.split('/');
        let action = match parts.next()? {
            "diary.history.edit" => Action::Edit,
            "diary.history.close" => Action::Close,
            "diary.history.delete" => Action::Delete,
            _ => return None,
        };
        let id = parts.next()?.parse().ok()?;
        let revision = parts.next()?.parse().ok()?;
        (revision > 0 && parts.next().is_none()).then_some(Self {
            action,
            id,
            revision,
        })
    }
}

impl Database {
    /// Process a signed history action and queue its reply in the same transaction.
    pub async fn apply_telegram_history_selection(
        &self,
        lease: &TelegramInboundLease,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        if envelope.channel().channel() != "telegram" || envelope.message_id() != lease.message_id {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        let selected = Selection::from_envelope(&envelope)
            .ok_or(TelegramDiaryCommitError::UnsupportedMessage)?;
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            let now =
                StoredInstant::try_from(at).map_err(|_| TelegramDiaryCommitError::Database)?;
            let instance = envelope.channel().instance_id();
            let page = HistoryPage {
                open_only: false,
                before: None,
            };
            let blocked = prompt::blocked(&mut txn, binding, lease, &envelope, now).await?;
            let current = if blocked.is_none() {
                rows(
                    &mut txn,
                    &format!("telegram:{instance}"),
                    page,
                    now,
                    Some(selected.id),
                )
                .await?
                .pop()
            } else {
                None
            };
            let mut pending = None;
            let message = if let Some(text) = blocked {
                text.to_owned()
            } else {
                match current
                    .as_ref()
                    .filter(|current| current.revision == selected.revision)
                {
                    None => unavailable(&mut txn, selected.id, selected.revision).await?,
                    Some(current) if selected.action == Action::Delete && current.can_delete => {
                        let actor = format!("telegram:{}", envelope.sender_ref());
                        Self::delete_event_in(
                            &mut txn,
                            DeleteEvent {
                                owner_id: binding.owner_id,
                                authenticated_actor: &actor,
                                event_id: selected.id,
                                expected_revision: selected.revision,
                            },
                            Some(lease.operation_id),
                        )
                        .await
                        .map_err(TelegramDiaryCommitError::Diary)?;
                        clear_selected_record(&mut txn, binding, selected.id)
                            .await
                            .map_err(TelegramDiaryCommitError::Clarification)?;
                        "Запись удалена. Отменить последнее изменение: /undo.".to_owned()
                    }
                    Some(current) if selected.action != Action::Delete && current.can_update => {
                        if current.kind.starts_with("user.") {
                            return Err(TelegramDiaryCommitError::UnsupportedMessage);
                        }
                        let (text, state) = selected_record(current, selected.action, now)?;
                        pending = Some(state);
                        text
                    }
                    Some(_) => "Доступ к этой записи изменился. Откройте /history заново.".into(),
                }
            };
            let actions = if pending.is_some() {
                let page_action = if selected.action == Action::Close {
                    OPEN_MIGRAINES
                } else {
                    ALL_HISTORY
                };
                json!([{"label":"Это не оно — история","action_id":page_action,
                    "operation_id":lease.operation_id.to_string()}])
            } else {
                json!([])
            };
            let intent = OutboundIntent::normalize(json!({
                "owner_id":binding.owner_id.to_string(),
                "conversation_id":binding.conversation_id.to_string(),
                "channel_instance":{"channel":"telegram","instance_id":instance},
                "blocks":[{"text":message}],"actions":actions,
            }))
            .map_err(|_| TelegramDiaryCommitError::Reply)?;
            let dedup = format!("inbound:{}:history-selection", lease.message_id);
            let queued = queue_outbox_intent(
                &mut txn,
                QueueIntent {
                    intent,
                    operation_id: lease.operation_id,
                    inbound_id: Some(lease.message_id),
                    dedup_key: Some(&dedup),
                    requirements: &[],
                    now,
                },
            )
            .await
            .map_err(TelegramDiaryCommitError::Outbox)?;
            if let Some(mut state) = pending {
                let token: String = sqlx::query_scalar(
                    "SELECT intent#>>'{actions,0,token}' FROM outbox_messages WHERE id=$1",
                )
                .bind(queued.outbox_id)
                .fetch_one(&mut *txn)
                .await?;
                state["selection_prompt"] = json!(token);
                record_history_selection(&mut txn, binding, state)
                    .await
                    .map_err(TelegramDiaryCommitError::Clarification)?;
            }
            let at = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            complete(&mut txn, lease, at, TelegramInboundState::Processed).await?;
            txn.commit().await?;
            Ok(queued)
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

async fn unavailable(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    revision: i32,
) -> Result<String, sqlx::Error> {
    let exists: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM events WHERE id=$1 AND revision=$2 AND NOT deleted)",
    )
    .bind(id)
    .bind(revision)
    .fetch_one(&mut **txn)
    .await?;
    let message = if exists {
        "Доступ к этой записи изменился. Откройте /history заново."
    } else {
        "Запись уже изменилась. Откройте /history и выберите её снова."
    };
    Ok(message.into())
}

fn selected_record(
    record: &Record,
    action: Action,
    now: StoredInstant,
) -> Result<(String, Value), TelegramDiaryCommitError> {
    let end = record
        .ended_at
        .map(StoredInstant::try_from)
        .transpose()
        .map_err(|_| TelegramDiaryCommitError::Database)?;
    let payload =
        serde_json::from_str(&record.payload).map_err(|_| TelegramDiaryCommitError::Database)?;
    let label = labels::label(&record.kind, &payload, end, now)
        .map_err(|_| TelegramDiaryCommitError::Database)?
        .chars()
        .take(200)
        .collect::<String>();
    let expires = OffsetDateTime::from(now) + time::Duration::minutes(15);
    let expires =
        StoredInstant::try_from(expires).map_err(|_| TelegramDiaryCommitError::Database)?;
    let (action, question, instructions) = if action == Action::Close {
        let text = "Во сколько закончилась мигрень?";
        ("close", text, text)
    } else {
        ("update", "Что исправить?", "Напишите, что исправить.")
    };
    Ok((
        format!(
            "Выбрано: {} — {label}. {instructions}",
            record.display_start
        ),
        json!({
            "text":"Исправить выбранную запись","question":question,"event_ids":[record.id.to_string()],
            "selection_revision":record.revision,"selected_at":now.to_string(),
            "explicit_selector":true,"targets_complete":true,"selection_expires_at":expires.to_string(),
            "action":action,"created_at":now.to_string(),
        }),
    ))
}
