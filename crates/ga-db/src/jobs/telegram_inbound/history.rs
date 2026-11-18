mod labels;
mod legacy;
mod prompt;
mod selection;

pub(crate) use legacy::resolve_legacy_history;

use std::{collections::BTreeSet, time::Duration};

use ga_domain::{
    channel::{InboundEnvelope, InboundKind, OutboundIntent},
    telegram_control::COMMAND_WHITESPACE,
    timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{FromRow, Postgres, Row, Transaction, postgres::PgRow};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{TelegramDiaryCommitError, TelegramInboundLease, TelegramInboundState, complete};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    diary::clear_history_selection,
    outbox_enqueue::{QueueIntent, QueuedIntent, queue_outbox_intent},
};

const ALL_HISTORY: &str = "diary.history.page";
const OPEN_MIGRAINES: &str = "diary.history.open";
const EXPIRED_HISTORY: &str = "diary.history.expired";
const PAGE_LENGTH: usize = 10;

#[derive(Clone, Copy)]
struct HistoryPage {
    open_only: bool,
    before: Option<(StoredInstant, Uuid)>,
}

impl HistoryPage {
    fn from_update(envelope: &InboundEnvelope) -> Option<Self> {
        match envelope.kind() {
            InboundKind::Text | InboundKind::Voice => {
                let command = envelope
                    .text()?
                    .split(|character| COMMAND_WHITESPACE.contains(character))
                    .find(|word| !word.is_empty())?;
                (command == "/history").then_some(Self {
                    open_only: false,
                    before: None,
                })
            }
            InboundKind::Action => {
                let stored = envelope.document();
                let mut parts = stored["action"]["action_id"].as_str()?.split('/');
                let open_only = match parts.next()? {
                    ALL_HISTORY => false,
                    OPEN_MIGRAINES => true,
                    _ => return None,
                };
                let before = match parts.next() {
                    None => None,
                    Some(timestamp) => Some((
                        StoredInstant::from_unix_microseconds(timestamp.parse().ok()?).ok()?,
                        parts.next()?.parse().ok()?,
                    )),
                };
                parts.next().is_none().then_some(Self { open_only, before })
            }
            _ => None,
        }
    }

    fn page_action(self) -> &'static str {
        if self.open_only {
            OPEN_MIGRAINES
        } else {
            ALL_HISTORY
        }
    }
}

struct Record {
    id: Uuid,
    revision: i32,
    kind: String,
    payload: String,
    start: OffsetDateTime,
    ended_at: Option<OffsetDateTime>,
    topology: String,
    display_start: String,
    definition_id: Option<Uuid>,
    version_id: Option<Uuid>,
    can_update: bool,
    can_delete: bool,
}

impl FromRow<'_, PgRow> for Record {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            id: row.try_get("id")?,
            revision: row.try_get("revision")?,
            kind: row.try_get("kind")?,
            payload: row.try_get("payload")?,
            start: row.try_get("start")?,
            ended_at: row.try_get("ended_at")?,
            topology: row.try_get("topology")?,
            display_start: row.try_get("display_start")?,
            definition_id: row.try_get("definition_id")?,
            version_id: row.try_get("version_id")?,
            can_update: row.try_get("can_update")?,
            can_delete: row.try_get("can_delete")?,
        })
    }
}

async fn rows(
    txn: &mut Transaction<'_, Postgres>,
    destination: &str,
    page: HistoryPage,
    now: StoredInstant,
    selected: Option<Uuid>,
) -> Result<Vec<Record>, sqlx::Error> {
    sqlx::query_as(concat!(
        include_str!("history/rows.sql"),
        include_str!("../../share_policy/consent.sql"),
        "))))
         AND (NOT $2 OR (entry.kind='migraine' AND entry.status='confirmed'
            AND entry.start <= $3 AND (entry.\"end\" IS NULL OR entry.\"end\">$3)))
         AND ($4::timestamptz IS NULL OR (entry.start,entry.id)<($4,$5::uuid))
         AND ($6::uuid IS NULL OR entry.id=$6)
         ORDER BY entry.start DESC,entry.id DESC LIMIT 11",
    ))
    .bind(destination)
    .bind(page.open_only)
    .bind(OffsetDateTime::from(now))
    .bind(page.before.map(|(before, _)| OffsetDateTime::from(before)))
    .bind(page.before.map(|(_, id)| id))
    .bind(selected)
    .fetch_all(&mut **txn)
    .await
}

impl Database {
    pub async fn apply_telegram_history(
        &self,
        lease: &TelegramInboundLease,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        if HistoryPage::from_update(&envelope).is_some() || expired_history(&envelope) {
            self.apply_telegram_history_page(lease).await
        } else {
            self.apply_telegram_history_selection(lease).await
        }
    }

    /// Process a diary history request from an authenticated Telegram inbox lease.
    pub async fn apply_telegram_history_page(
        &self,
        lease: &TelegramInboundLease,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        let page = HistoryPage::from_update(&envelope);
        let expired = expired_history(&envelope);
        if page.is_none() && !expired {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        if envelope.channel().channel() != "telegram" || envelope.message_id() != lease.message_id {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            let timestamp =
                StoredInstant::try_from(now).map_err(|_| TelegramDiaryCommitError::Database)?;
            let instance = envelope.channel().instance_id();
            let blocked = prompt::blocked(&mut txn, binding, lease, &envelope, timestamp).await?;
            let intent = if let Some(text) =
                blocked.or(expired.then_some("Эта кнопка устарела. Откройте /history заново."))
            {
                OutboundIntent::normalize(json!({
                    "owner_id":binding.owner_id.to_string(),
                    "conversation_id":binding.conversation_id.to_string(),
                    "channel_instance":{"channel":"telegram","instance_id":instance},
                    "blocks":[{"text":text}],
                }))
                .map_err(|_| TelegramDiaryCommitError::Reply)?
            } else {
                let page = page.ok_or(TelegramDiaryCommitError::UnsupportedMessage)?;
                clear_history_selection(&mut txn, binding)
                    .await
                    .map_err(TelegramDiaryCommitError::Clarification)?;
                let records = rows(
                    &mut txn,
                    &format!("telegram:{instance}"),
                    page,
                    timestamp,
                    None,
                )
                .await?;
                history_reply(
                    binding,
                    instance,
                    page,
                    timestamp,
                    &records,
                    lease.operation_id,
                )?
            };
            let dedup = format!("inbound:{}:history", lease.message_id);
            let queued = queue_outbox_intent(
                &mut txn,
                QueueIntent {
                    intent,
                    operation_id: lease.operation_id,
                    inbound_id: Some(lease.message_id),
                    dedup_key: Some(&dedup),
                    requirements: &[],
                    now: timestamp,
                },
            )
            .await
            .map_err(TelegramDiaryCommitError::Outbox)?;
            let ended_at = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            complete(&mut txn, lease, ended_at, TelegramInboundState::Processed).await?;
            txn.commit().await?;
            Ok(queued)
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

fn expired_history(envelope: &InboundEnvelope) -> bool {
    envelope.kind() == InboundKind::Action
        && envelope.document()["action"]["action_id"] == EXPIRED_HISTORY
}

fn history_reply(
    binding: ConversationBinding,
    instance: &str,
    page: HistoryPage,
    now: StoredInstant,
    records: &[Record],
    operation: Uuid,
) -> Result<OutboundIntent, TelegramDiaryCommitError> {
    let mut lines = Vec::new();
    let mut actions = Vec::new();
    let mut tracker_refs = BTreeSet::new();
    let button = |name: &str, action: String| {
        json!({
            "label": name, "action_id": action, "operation_id": operation.to_string()
        })
    };
    for (index, record) in records.iter().take(PAGE_LENGTH).enumerate() {
        let number = index + 1;
        let end = record
            .ended_at
            .map(StoredInstant::try_from)
            .transpose()
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        let payload = serde_json::from_str(&record.payload)
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        let label = labels::label(&record.kind, &payload, end, now)
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        let label = label.chars().take(160).collect::<String>();
        lines.push(format!("{number}. {} — {label}", record.display_start));
        if record.kind.starts_with("user.") {
            let (Some(definition), Some(version)) = (record.definition_id, record.version_id)
            else {
                return Err(TelegramDiaryCommitError::Database);
            };
            tracker_refs.insert(format!("definition:{definition}"));
            tracker_refs.insert(format!("definition-version:{version}"));
        }
        let select = |action| format!("diary.history.{action}/{}/{}", record.id, record.revision);
        if record.can_update {
            let (label, route) = if page.open_only {
                ("Завершить", "close")
            } else {
                ("Исправить", "edit")
            };
            actions.push(button(&format!("{number}: {label}"), select(route)));
            if record.kind.starts_with("user.")
                && !page.open_only
                && record.topology == "open_interval"
                && end.is_none()
                && record.start <= OffsetDateTime::from(now)
            {
                actions.push(button(&format!("{number}: Завершить"), select("close")));
            }
        }
        if record.can_delete {
            actions.push(button(&format!("{number}: Удалить"), select("delete")));
        }
    }
    actions.push(button("Сначала", page.page_action().into()));
    if records.len() > PAGE_LENGTH {
        let cursor = &records[PAGE_LENGTH - 1];
        let before = StoredInstant::try_from(cursor.start)
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        actions.push(button(
            "Далее",
            format!(
                "{}/{}/{}",
                page.page_action(),
                before.unix_microseconds(),
                cursor.id
            ),
        ));
    }
    let heading = if page.open_only {
        "Выберите эпизод:"
    } else {
        "История дневника:"
    };
    let body = if lines.is_empty() {
        "Записей нет.".into()
    } else {
        lines.join("\n")
    };
    OutboundIntent::normalize(json!({
        "owner_id": binding.owner_id.to_string(), "conversation_id": binding.conversation_id.to_string(),
        "channel_instance":{"channel":"telegram","instance_id":instance},
        "blocks":[{"text":format!("{heading}\n{body}")}], "actions":actions,
        "evidence_refs":tracker_refs,
    })).map_err(|_| TelegramDiaryCommitError::Reply)
}
