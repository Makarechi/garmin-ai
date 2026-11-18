use ga_domain::{
    channel::{InboundEnvelope, OutboundIntent},
    event_input::{EventInput, ValidationMode, source_offset_datetime},
    timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    Conversation, DESTINATION, OutboxRow, WebChatError, WebChatReply, WebChatSnapshot,
    binding_document, can_show, prepare, timestamp,
};
use crate::{
    Database,
    channel_ingress::{InboundReceipt, ingest},
    diary::CreateSystemEvent,
    health_analysis::owner_timezone,
    outbox_enqueue::{QueueIntent, queue_outbox_intent},
    proactive_control::{self, ProactiveCommand},
};

pub struct WebChatSubmission<'a> {
    pub client_message_id: Uuid,
    pub text: &'a str,
    pub configured_timezone: &'a str,
    pub configured_locale: &'a str,
    pub urgent_messages: Option<(&'a str, &'a str)>,
}

impl Database {
    /// Keep one owner message, its local diary changes, and its reply in a single transaction.
    pub async fn send_web_chat(
        &self,
        request: WebChatSubmission<'_>,
    ) -> Result<WebChatSnapshot, WebChatError> {
        if !(1..=16_000).contains(&request.text.chars().count()) {
            return Err(WebChatError::Invalid);
        }
        let receipt = tokio::time::timeout(std::time::Duration::from_secs(4), async {
            let mut txn = self.work.begin().await?;
            let conversation = prepare(&mut txn, true).await?;
            let received: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn).await?;
            let at = timestamp(received)?;
            let external = request.client_message_id.to_string();
            let envelope = json!({
                "owner_id":conversation.owner.to_string(),
                "conversation_id":conversation.id.to_string(),
                "channel_instance":{"channel":"web","instance_id":"local"},
                "external_event_id":external,"external_message_id":external,
                "sender_ref":"owner","received_at":at.to_string(),"kind":"text","text":request.text,
            });
            let raw = serde_json::to_vec(&envelope).map_err(|_| WebChatError::Invalid)?;
            let envelope = InboundEnvelope::parse(&raw).map_err(|_| WebChatError::Invalid)?;
            let receipt = ingest(&mut txn, &envelope).await.map_err(|_| WebChatError::Database)?;
            if receipt.created {
                let (reply, facts) = local_reply(&mut txn, conversation, &request, &receipt, at).await?;
                let intent = OutboundIntent::normalize(json!({
                    "owner_id":conversation.owner.to_string(),"conversation_id":conversation.id.to_string(),
                    "channel_instance":{"channel":"web","instance_id":"local"},
                    "blocks":[{"text":reply}],"evidence_refs":facts,
                })).map_err(|_| WebChatError::Database)?;
                queue_outbox_intent(&mut txn, QueueIntent {
                    intent,operation_id:receipt.operation_id,inbound_id:Some(receipt.message_id),
                    dedup_key:None,requirements:&[],now:at,
                }).await.map_err(|_| WebChatError::Database)?;
                sqlx::query("UPDATE inbound_messages SET status='processed' WHERE id=$1")
                    .bind(receipt.message_id).execute(&mut *txn).await?;
            } else {
                let normalized: Option<String> = sqlx::query_scalar(
                    "SELECT normalized_text FROM inbound_messages WHERE id=$1",
                ).bind(receipt.message_id).fetch_one(&mut *txn).await?;
                if normalized.as_deref() != Some(request.text) {
                    return Err(WebChatError::ReusedMessage);
                }
            }
            txn.commit().await?;
            Ok(receipt)
        }).await.map_err(|_| WebChatError::Database)??;
        self.web_chat_submission_reply(receipt).await
    }

    async fn web_chat_submission_reply(
        &self,
        receipt: InboundReceipt,
    ) -> Result<WebChatSnapshot, WebChatError> {
        let mut txn = self.work.begin_with("BEGIN READ ONLY").await?;
        let conversation = prepare(&mut txn, false).await?;
        let row: Option<OutboxRow> = sqlx::query_as(
            "SELECT id,inbound_message_id,state,created_at,
                CASE WHEN jsonb_typeof(intent->'blocks')='array' THEN jsonb_array_length(intent->'blocks') ELSE 0 END,
                (intent - ARRAY['blocks','attachments','form','actions'])::text
             FROM outbox_messages WHERE owner_id=$1 AND conversation_id=$2 AND inbound_message_id=$3
             AND state IN ('queued','read') ORDER BY created_at,id LIMIT 1",
        ).bind(conversation.owner).bind(conversation.id).bind(receipt.message_id)
         .fetch_optional(&mut *txn).await?;
        let mut replies = std::collections::VecDeque::new();
        if let Some((id, inbound, state, at, count, raw)) = row
            && let Some(document) = binding_document(conversation, id, &raw)
            && can_show(&mut txn, &state, &document).await?
        {
            replies.push_back((
                id,
                count,
                WebChatReply {
                    id: id.to_string(),
                    inbound_message_id: inbound.map(|id| id.to_string()),
                    state,
                    created_at: timestamp(at)?.to_string(),
                },
            ));
        }
        let mut opening = format!(
            "{{\"duplicate\":{},\"message_id\":\"{}\",\"reply\":",
            !receipt.created, receipt.message_id,
        )
        .into_bytes();
        let closing = if replies.is_empty() {
            opening.extend_from_slice(b"null}");
            None
        } else {
            Some(b"}".as_slice())
        };
        Ok(WebChatSnapshot {
            opening: Some(opening),
            replies,
            active: None,
            first: true,
            closing,
            permissions: txn,
        })
    }
}

async fn local_reply(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Conversation,
    request: &WebChatSubmission<'_>,
    inbound: &InboundReceipt,
    at: StoredInstant,
) -> Result<(String, Vec<String>), WebChatError> {
    if let Some((russian, english)) = request.urgent_messages {
        let saved: Option<String> = sqlx::query_scalar(
            "SELECT locale FROM people WHERE id=$1
               AND EXISTS(SELECT 1 FROM app_state WHERE key='preferences:onboarding')",
        )
        .bind(conversation.owner)
        .fetch_optional(&mut **txn)
        .await?;
        let locale = saved.as_deref().unwrap_or(request.configured_locale);
        let russian_locale = locale
            .split(['-', '_'])
            .next()
            .is_some_and(|code| code.eq_ignore_ascii_case("ru"));
        return Ok((
            (if russian_locale { russian } else { english }).into(),
            Vec::new(),
        ));
    }
    let input = request.text.trim();
    let text = match input {
        "/help" => {
            "Команды: /note текст, /history, /pause, /resume, /cancel. Формы и дневник доступны на /dashboard."
        }
        "/history" => {
            let zone = owner_timezone(txn, request.configured_timezone)
                .await
                .map_err(|_| WebChatError::Database)?;
            return history(txn, &zone).await;
        }
        "/pause" | "/resume" => {
            let state = proactive_control::set_in(
                txn,
                ProactiveCommand::Web { received_at: at },
                input == "/resume",
            )
            .await
            .map_err(|_| WebChatError::Database)?;
            if state.enabled {
                "Инициативные сообщения включены."
            } else {
                "Инициативные сообщения остановлены."
            }
        }
        "/cancel" => {
            sqlx::query("DELETE FROM app_state WHERE key IN (
                         'conversation:pending:web:local','analysis:conversation:pending:web:local')")
                .execute(&mut **txn).await?;
            "Текущее уточнение отменено."
        }
        note if note.starts_with("/note ") => {
            let content = note.strip_prefix("/note ").unwrap_or(note).trim();
            let timezone = owner_timezone(txn, request.configured_timezone)
                .await
                .map_err(|_| WebChatError::Database)?;
            let event = json!({
                "start":at.to_string(),"timezone":timezone,"source":"manual",
                "original_text":content,"payload":{"type":"note","description":content},
            });
            let event = serde_json::to_vec(&event).map_err(|_| WebChatError::Invalid)?;
            let event = EventInput::from_json(&event, 512_000, ValidationMode::NewReport)
                .map_err(|_| WebChatError::Invalid)?;
            event
                .validate_for_write()
                .map_err(|_| WebChatError::Invalid)?;
            Database::create_system_event_in(
                txn,
                CreateSystemEvent {
                    owner_id: conversation.owner,
                    authenticated_actor: DESTINATION,
                    report: &event,
                    idempotency_key: Some(&format!("web-chat:{}", request.client_message_id)),
                    operation_id: Some(inbound.operation_id),
                    clock_uncertainty_seconds: None,
                },
            )
            .await
            .map_err(|_| WebChatError::Database)?;
            "Заметка сохранена. Откройте дневник, чтобы просмотреть или исправить её."
        }
        _ => "Модель недоступна. Для записи используйте /note текст или формы на /dashboard.",
    };
    Ok((text.into(), Vec::new()))
}

async fn history(
    txn: &mut Transaction<'_, Postgres>,
    timezone: &str,
) -> Result<(String, Vec<String>), WebChatError> {
    let rows: Vec<(String, OffsetDateTime, Option<Uuid>, Option<Uuid>)> = sqlx::query_as(concat!(
        "WITH destination(kind,instance,categories) AS (
           VALUES ('channel'::text, $1::text, '[\"schema\",\"facts\"]'::jsonb)
         )
         SELECT entry.kind,entry.start,version.definition_id,version.id ",
        include_str!("../share_policy/entry_visibility.sql"),
        include_str!("../share_policy/consent.sql"),
        ")))) ORDER BY entry.start DESC,entry.id DESC LIMIT 5",
    ))
    .bind(DESTINATION)
    .fetch_all(&mut **txn)
    .await?;
    if rows.is_empty() {
        return Ok((
            "Пока нет доступных записей. Откройте дневник для подробностей.".into(),
            Vec::new(),
        ));
    }
    let mut labels = Vec::with_capacity(rows.len());
    let mut dependencies = std::collections::BTreeSet::new();
    for (kind, start, definition, version) in rows {
        let display =
            source_offset_datetime(timestamp(start)?, timezone).ok_or(WebChatError::Database)?;
        labels.push(format!(
            "{kind} ({:02}.{:02} {:02}:{:02})",
            display.day(),
            u8::from(display.month()),
            display.hour(),
            display.minute()
        ));
        if kind.starts_with("user.") {
            let definition = definition.ok_or(WebChatError::Database)?;
            let version = version.ok_or(WebChatError::Database)?;
            dependencies.insert(format!("definition:{definition}"));
            dependencies.insert(format!("definition-version:{version}"));
        }
    }
    Ok((
        format!(
            "Последние записи: {}. Подробности и исправления доступны в дневнике.",
            labels.join("; ")
        ),
        dependencies.into_iter().collect(),
    ))
}
