use std::{error::Error, fmt, time::Duration};

use ga_domain::{
    channel::{InboundEnvelope, OutboundIntent},
    telegram_control::TelegramControl,
    timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    InboxLane, KIND, TelegramInboundLease, TelegramInboundState, complete, superseded,
    try_ingress_lock,
};
use crate::{
    Database,
    channel_ingress::telegram_poll::{TelegramPollError, selection},
    conversation_memory::{ConversationBinding, MemoryError, forget_telegram_in},
    jobs::{JobStoreError, begin},
    outbox_enqueue::{QueueIntent, QueueIntentError, QueuedIntent, queue_outbox_intent},
    proactive_control::{ProactiveCommand, ProactiveControlError, set_in},
    replay::REPLAY_MUTATION_LOCK,
    telegram_binding::{TelegramBindingError, confirmed_pairing},
    telegram_today,
};

#[derive(Debug)]
pub enum TelegramControlError {
    Job(JobStoreError),
    Channel(TelegramPollError),
    Pairing(TelegramBindingError),
    Memory(MemoryError),
    Proactive(ProactiveControlError),
    Outbox(QueueIntentError),
    BindingChanged,
    Unsupported,
    Malformed,
    Database,
}

impl fmt::Display for TelegramControlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Job(error) => error.fmt(f),
            Self::Channel(error) => error.fmt(f),
            Self::Pairing(error) => error.fmt(f),
            Self::Memory(error) => error.fmt(f),
            Self::Proactive(error) => error.fmt(f),
            Self::Outbox(error) => error.fmt(f),
            Self::BindingChanged => f.write_str("Telegram owner or selected channel changed"),
            Self::Unsupported => f.write_str("The command requires a separate handler"),
            Self::Malformed => f.write_str("The stored Telegram command is invalid"),
            Self::Database => f.write_str("The Telegram command could not be committed"),
        }
    }
}
impl Error for TelegramControlError {}
impl From<sqlx::Error> for TelegramControlError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Apply a local owner setting or forget command and queue its confirmation
    /// in the same transaction that consumes the current authenticated lease.
    pub async fn apply_telegram_owner_control(
        &self,
        lease: &TelegramInboundLease,
        question_budget: u16,
    ) -> Result<QueuedIntent, TelegramControlError> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| TelegramControlError::Malformed)?;
        if envelope.message_id() != lease.message_id || envelope.channel().channel() != "telegram" {
            return Err(TelegramControlError::Malformed);
        }
        let command = match TelegramControl::from_envelope(&envelope) {
            Some(
                command @ (TelegramControl::Pause
                | TelegramControl::Resume
                | TelegramControl::Today
                | TelegramControl::Help
                | TelegramControl::Start
                | TelegramControl::ForgetConversation),
            ) => command,
            _ => return Err(TelegramControlError::Unsupported),
        };
        let chat = envelope
            .sender_ref()
            .parse()
            .map_err(|_| TelegramControlError::Malformed)?;
        let owner = envelope.owner_id();
        let instance = envelope.channel().instance_id();
        let conversation = envelope.conversation_id();

        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = begin(&self.control).await.map_err(TelegramControlError::Job)?;
            if !try_ingress_lock(&mut txn).await.map_err(TelegramControlError::Job)? {
                return Err(TelegramControlError::Database);
            }
            if !selection(&mut txn, instance).await.map_err(TelegramControlError::Channel)?
                || confirmed_pairing(&mut txn, instance, chat).await.map_err(TelegramControlError::Pairing)? != owner
            {
                return Err(TelegramControlError::BindingChanged);
            }
            let ordering_epoch = control_epoch(&mut txn, lease).await?;
            let mut projection = None;
            let text = match command {
                TelegramControl::Help | TelegramControl::Start => HELP.to_owned(),
                TelegramControl::Today => {
                    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                        .bind(REPLAY_MUTATION_LOCK).fetch_one(&mut *txn).await?;
                    if !locked {
                        return Err(TelegramControlError::Database);
                    }
                    let (text, fence) = telegram_today::capture(&mut txn, conversation).await?;
                    projection = Some(fence);
                    text
                }
                TelegramControl::ForgetConversation => {
                    forget_telegram_in(&mut txn, ConversationBinding { owner_id: owner, conversation_id: conversation })
                        .await.map_err(TelegramControlError::Memory)?;
                    "Контекст аналитического разговора очищен. Записи дневника сохранены.".to_owned()
                }
                TelegramControl::Pause | TelegramControl::Resume => {
                    let received_at = StoredInstant::try_from(envelope.received_at())
                        .map_err(|_| TelegramControlError::Malformed)?;
                    let message_at = envelope.occurred_at().map(StoredInstant::try_from).transpose()
                        .map_err(|_| TelegramControlError::Malformed)?.unwrap_or(received_at);
                    let update_id = envelope.external_event_id().parse().map_err(|_| TelegramControlError::Malformed)?;
                    let effective = set_in(&mut txn, ProactiveCommand::Telegram {
                        message_at, received_at, ordering_epoch, update_id,
                    }, command == TelegramControl::Resume).await.map_err(TelegramControlError::Proactive)?;
                    if effective.enabled {
                        format!("Настройка сохранена: вопросы разрешены. Лимит в день: {question_budget}, только вне тихих часов.")
                    } else {
                        "Вопросы отключены. Синхронизация продолжается.".to_owned()
                    }
                }
                _ => return Err(TelegramControlError::Unsupported),
            };
            let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn).await?;
            let now = now.replace_nanosecond(now.microsecond() * 1000).map_err(|_| TelegramControlError::Database)?;
            let timestamp = StoredInstant::try_from(now).map_err(|_| TelegramControlError::Database)?;
            let intent = json!({
                "intent_id":Uuid::new_v4().to_string(), "owner_id":owner.to_string(), "conversation_id":conversation.to_string(),
                "channel_instance":{"channel":"telegram","instance_id":instance},
                "blocks":[{"text":text}],
            });
            let intent = OutboundIntent::normalize(intent)
                .map_err(|_| TelegramControlError::Malformed)?;
            let outbox = queue_outbox_intent(&mut txn, QueueIntent {
                intent, operation_id:lease.operation_id, inbound_id:Some(lease.message_id),
                dedup_key:None, requirements:&[], now:timestamp,
            }).await.map_err(TelegramControlError::Outbox)?;
            if outbox.created && let Some(projection) = projection {
                sqlx::query("UPDATE outbox_messages SET memory_fence=$2::jsonb WHERE id=$1")
                    .bind(outbox.outbox_id).bind(projection).execute(&mut *txn).await?;
            }
            // Recheck expiry after storage work, while every side effect is still reversible.
            let final_clock: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn).await?;
            complete(&mut txn, lease, final_clock, TelegramInboundState::Processed).await
                .map_err(TelegramControlError::Job)?;
            txn.commit().await?;
            Ok(outbox)
        })
        .await
        .map_err(|_| TelegramControlError::Database)?
    }
}

const HELP: &str = concat!(
    "Готов вести ваш дневник и анализировать Garmin. Пишите, например: «кофе в 11» или «как я восстановился?»\n\n",
    "/today — последние показатели\n/status — состояние синхронизации\n/history — записи дневника\n",
    "/newtracker — создать трекер\n/goals — личные цели\n/undo — отменить последнее изменение\n",
    "/cancel — отменить уточнение\n/pause — отключить вопросы\n/resume — включить вопросы\n\n",
    "ИИ-модель сейчас недоступна; дневник и формы доступны локально. Полная исходная история хранится локально. ",
    "Наблюдения по данным не являются диагнозом.\n/conversation — контекст анализа\n",
    "/forget_conversation — очистить контекст анализа\n",
    "/debug — состояние диагностики; /debug on и /debug off — уведомления об ошибках",
);

async fn control_epoch(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
) -> Result<i64, TelegramControlError> {
    let epoch: Option<i64> = sqlx::query_scalar(
        "SELECT coalesce((j.payload->>'ordering_epoch')::bigint,0)
         FROM jobs j JOIN inbound_messages i ON j.dedup_key=concat('telegram:inbound:native:',i.id)
         WHERE j.id=$1 AND j.lease_token=$2 AND j.kind=$3 AND j.status='running'
          AND j.lease_until>clock_timestamp() AND i.id=$4 AND i.status='pending'
          AND i.envelope=$5::jsonb AND i.operation_id=$6 AND i.legacy_telegram_update_id IS NULL
          AND j.payload->>'lane'=$7 AND j.payload->>'inbound_message_id'=i.id::text
         FOR UPDATE OF j",
    )
    .bind(lease.job_id)
    .bind(lease.token)
    .bind(KIND)
    .bind(lease.message_id)
    .bind(lease.envelope_json())
    .bind(lease.operation_id)
    .bind(InboxLane::Control.label())
    .fetch_optional(&mut **txn)
    .await?;
    if let Some(epoch) = epoch
        && !superseded(txn, lease.message_id)
            .await
            .map_err(TelegramControlError::Job)?
    {
        return Ok(epoch);
    }
    Err(TelegramControlError::Job(JobStoreError::LeaseLost))
}
