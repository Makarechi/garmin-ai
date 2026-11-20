use std::time::Duration;

use ga_domain::{
    agent_interpretation::{AgentIntent, AgentInterpretation},
    channel::InboundEnvelope,
    event_input::EventSource,
    localization::catalog_text,
    model_consent::ModelCategories,
    timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::analysis::WebAnalysisProof;
use super::send::{finish_web_chat_message, web_chat_envelope};
use super::{WebChatError, WebChatSnapshot, WebChatSubmission, prepare, timestamp};
use crate::{
    Database,
    channel_ingress::{InboundReceipt, ensure_conversation, ingest},
    conversation_memory::{ConversationBinding, MemoryFence},
    diary::{
        ClarificationClock, ClarificationReply, CreateAgentLogBatch, InterpretationContext,
        InterpretationContextQuery, StoredSystemEvent, UndoDiaryOperation,
        agent_correction::{AgentCorrection, AgentCorrectionContext},
        analysis_evidence::DiaryAnalysisFence,
        cancel_in, interpretation_context_in, save_clarification_in,
        undo::{latest_undo_audit, try_undo_and_reconcile},
    },
    health_analysis::owner_timezone,
    model_gate::onboarding::onboarding_allows_model,
    share_policy::{
        DestinationKind, MODEL_CONSENT_LOCK, OWNER_ONBOARDING_LOCK, ShareDestination,
        try_shared_lock,
    },
};

const MODEL_FAILED: &str = "Модель не смогла надёжно обработать сообщение. Ничего не сохранено; попробуйте форму в дневнике.";
const SAVE_FAILED: &str = "Не удалось применить запись или исправление. Ничего не сохранено; уточните данные или используйте форму в дневнике.";

#[derive(Clone, Copy)]
pub struct WebChatInterpretationContext<'a> {
    pub model: ShareDestination<'a>,
    pub digest: &'a str,
    pub mentioned_events: &'a [Uuid],
}

pub enum WebChatModelReply<'a> {
    Unverified,
    FixedNotice(&'static str),
    Clarify {
        question: &'a str,
        expected: WebChatInterpretationContext<'a>,
    },
    Mutation {
        command: &'a AgentInterpretation,
        expected: WebChatInterpretationContext<'a>,
    },
    Undo {
        command: &'a AgentInterpretation,
        language: &'a str,
        expected: WebChatInterpretationContext<'a>,
    },
    Analysis {
        answer: &'a str,
        memory: &'a MemoryFence,
        goals_revision: u64,
        evidence: &'a [Value],
        expected: WebChatInterpretationContext<'a>,
    },
}

pub enum WebChatModelPreparation {
    Ready(Box<WebChatModelSession>),
    Replied(WebChatSnapshot),
}

pub struct WebChatModelSession {
    // This transaction holds only the one submission lock, on the dedicated
    // model operation pool. Shared reads and unrelated writes stay available.
    lock: Transaction<'static, Postgres>,
    envelope: InboundEnvelope,
    pub at: StoredInstant,
    pub locale: String,
    pub timezone: String,
    undo_anchor: Option<i64>,
}

impl WebChatModelSession {
    pub fn clarification_clock(&self) -> ClarificationClock {
        ClarificationClock {
            message_at: self.at,
            conversation_now: self.at,
            use_message_time: false,
        }
    }

    pub fn binding(&self) -> ConversationBinding {
        ConversationBinding {
            owner_id: self.envelope.owner_id(),
            conversation_id: self.envelope.conversation_id(),
        }
    }
}

impl Database {
    /// Reserve one authenticated client message ID before disclosing any text.
    pub async fn prepare_web_chat_model(
        &self,
        request: &WebChatSubmission<'_>,
    ) -> Result<WebChatModelPreparation, WebChatError> {
        if !(1..=16_000).contains(&request.text.chars().count()) {
            return Err(WebChatError::Invalid);
        }
        tokio::time::timeout(Duration::from_secs(4), async {
            let mut lock = self.model_operations.begin().await?;
            let keyed = Uuid::new_v5(
                &Uuid::NAMESPACE_URL,
                format!("garmin-ai/web-chat-model/{}", request.client_message_id).as_bytes(),
            );
            let mut key = [0; 8];
            key.copy_from_slice(&keyed.as_bytes()[..8]);
            sqlx::query("SELECT pg_advisory_xact_lock($1)")
                .bind(i64::from_be_bytes(key))
                .execute(&mut *lock)
                .await?;

            let mut txn = self.work.begin().await?;
            let conversation = prepare(&mut txn, true).await?;
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            let at = timestamp(at)?;
            let envelope = web_chat_envelope(conversation, request, at)?;
            if let Some(receipt) = existing_receipt(&mut txn, &envelope).await? {
                txn.commit().await?;
                lock.rollback().await?;
                return self
                    .web_chat_submission_reply(receipt)
                    .await
                    .map(WebChatModelPreparation::Replied);
            }
            ensure_conversation(&mut txn, &envelope)
                .await
                .map_err(|_| WebChatError::Database)?;
            let timezone = owner_timezone(&mut txn, request.configured_timezone)
                .await
                .map_err(|_| WebChatError::Database)?;
            let saved: Option<String> = sqlx::query_scalar(
                "SELECT locale FROM people WHERE id=$1
                   AND EXISTS(SELECT 1 FROM app_state WHERE key='preferences:onboarding')",
            )
            .bind(conversation.owner)
            .fetch_optional(&mut *txn)
            .await?;
            let locale = saved.unwrap_or_else(|| request.configured_locale.to_owned());
            let undo_anchor = latest_undo_audit(&mut txn, super::DESTINATION).await?;
            txn.commit().await?;
            Ok(WebChatModelPreparation::Ready(Box::new(
                WebChatModelSession {
                    lock,
                    envelope,
                    at,
                    timezone,
                    locale,
                    undo_anchor,
                },
            )))
        })
        .await
        .map_err(|_| WebChatError::Database)?
    }

    pub async fn finish_web_chat_model(
        &self,
        session: WebChatModelSession,
        configured_timezone: &str,
        reply: WebChatModelReply<'_>,
        saved_message: impl FnOnce(&[StoredSystemEvent]) -> Option<String>,
    ) -> Result<WebChatSnapshot, WebChatError> {
        let receipt = tokio::time::timeout(Duration::from_secs(4), async {
            let mut txn = self.work.begin().await?;
            let conversation = prepare(&mut txn, true).await?;
            if conversation.owner != session.envelope.owner_id()
                || conversation.id != session.envelope.conversation_id()
            {
                return Err(WebChatError::Unavailable);
            }
            if let Some(receipt) = existing_receipt(&mut txn, &session.envelope).await? {
                txn.commit().await?;
                return Ok(receipt);
            }
            let receipt = ingest(&mut txn, &session.envelope)
                .await
                .map_err(|_| WebChatError::Database)?;
            let mut analysis_proof = None;
            let (message, facts) = render_model_reply(
                &mut txn,
                &session,
                &receipt,
                configured_timezone,
                reply,
                saved_message,
                &mut analysis_proof,
            )
            .await?;
            let reply = finish_web_chat_message(
                &mut txn,
                &session.envelope,
                &receipt,
                &message,
                &facts,
                session.at,
            )
            .await?;
            if let Some(proof) = analysis_proof {
                proof.store(&mut txn, reply).await?;
            }
            txn.commit().await?;
            Ok(receipt)
        })
        .await
        .map_err(|_| WebChatError::Database)??;
        // The completion is durable before releasing the duplicate submission lock.
        drop(session.lock);
        self.web_chat_submission_reply(receipt).await
    }
}

async fn render_model_reply(
    txn: &mut Transaction<'_, Postgres>,
    session: &WebChatModelSession,
    receipt: &InboundReceipt,
    configured_timezone: &str,
    reply: WebChatModelReply<'_>,
    saved_message: impl FnOnce(&[StoredSystemEvent]) -> Option<String>,
    analysis_proof: &mut Option<WebAnalysisProof>,
) -> Result<(String, Vec<String>), WebChatError> {
    let expected = match &reply {
        WebChatModelReply::Unverified => return Ok((MODEL_FAILED.to_owned(), Vec::new())),
        // The fixed safety notice uses no diary context or tracker references.
        WebChatModelReply::FixedNotice(message) => return Ok(((*message).to_owned(), Vec::new())),
        WebChatModelReply::Clarify { expected, .. }
        | WebChatModelReply::Mutation { expected, .. }
        | WebChatModelReply::Undo { expected, .. }
        | WebChatModelReply::Analysis { expected, .. } => *expected,
    };
    let Some(context) = current_interpretation(txn, session, configured_timezone, expected).await?
    else {
        return Ok((MODEL_FAILED.to_owned(), Vec::new()));
    };

    let mut versions = context.events.channel_versions;
    let message = match reply {
        WebChatModelReply::Unverified => MODEL_FAILED.to_owned(),
        WebChatModelReply::FixedNotice(message) => message.to_owned(),
        WebChatModelReply::Clarify { question, .. } => {
            save_clarification_in(
                txn,
                session.binding(),
                ClarificationReply {
                    clock: session.clarification_clock(),
                    text: session.envelope.text().ok_or(WebChatError::Invalid)?.trim(),
                    question,
                    selection_prompt: None,
                },
            )
            .await
            .map_err(|_| WebChatError::Database)?;
            question.to_owned()
        }
        WebChatModelReply::Mutation { command, .. } => {
            if context.selection_unavailable
                || (command.intent() == AgentIntent::Log
                    && context
                        .pending_clarification
                        .as_ref()
                        .is_some_and(|pending| pending["explicit_selector"] == true))
            {
                return Ok((SAVE_FAILED.to_owned(), Vec::new()));
            }
            let mut savepoint = txn.begin().await?;
            let saved = apply_model_mutation(
                &mut savepoint,
                session,
                receipt,
                command,
                &context.events.entries,
            )
            .await
            .and_then(|saved| saved_message(&saved));
            match saved {
                Some(message) => {
                    cancel_in(&mut savepoint, session.binding())
                        .await
                        .map_err(|_| WebChatError::Database)?;
                    savepoint.commit().await?;
                    message
                }
                None => {
                    savepoint.rollback().await?;
                    return Ok((SAVE_FAILED.to_owned(), Vec::new()));
                }
            }
        }
        WebChatModelReply::Undo {
            command, language, ..
        } => {
            if command.intent() != AgentIntent::Undo
                || command.confidence() < 0.85
                || session.undo_anchor.is_none()
                || latest_undo_audit(txn, super::DESTINATION).await? != session.undo_anchor
            {
                return Ok((SAVE_FAILED.to_owned(), Vec::new()));
            }
            let undone = try_undo_and_reconcile(
                txn,
                &UndoDiaryOperation {
                    owner_id: session.envelope.owner_id(),
                    authenticated_actor: super::DESTINATION,
                },
                configured_timezone,
            )
            .await
            .map_err(|_| WebChatError::Database)?;
            let Some(undone) = undone else {
                return Ok((SAVE_FAILED.to_owned(), Vec::new()));
            };
            cancel_in(txn, session.binding())
                .await
                .map_err(|_| WebChatError::Database)?;
            if undone.count > 1 {
                catalog_text(language, "agent.undo_many")
                    .ok_or(WebChatError::Database)?
                    .replace("{count}", &undone.count.to_string())
            } else {
                catalog_text(language, "agent.undo_one")
                    .ok_or(WebChatError::Database)?
                    .to_owned()
            }
        }
        WebChatModelReply::Analysis {
            answer,
            memory,
            goals_revision,
            evidence,
            ..
        } => {
            let Some(fence) = DiaryAnalysisFence::from_evidence(
                expected.model.instance(),
                &session.timezone,
                configured_timezone,
                goals_revision,
                evidence,
            ) else {
                return Ok((MODEL_FAILED.to_owned(), Vec::new()));
            };
            let Some(cited) = fence.check_web(txn, session.binding(), memory).await? else {
                return Ok((MODEL_FAILED.to_owned(), Vec::new()));
            };
            versions.extend(cited);
            *analysis_proof = Some(WebAnalysisProof::new(memory, fence));
            cancel_in(txn, session.binding())
                .await
                .map_err(|_| WebChatError::Database)?;
            answer.to_owned()
        }
    };
    let ids: Vec<_> = versions.into_iter().collect();
    let definitions: Vec<(Uuid, Uuid)> =
        sqlx::query_as("SELECT id,definition_id FROM event_definition_versions WHERE id=ANY($1)")
            .bind(&ids)
            .fetch_all(&mut **txn)
            .await?;
    if definitions.len() != ids.len() {
        return Err(WebChatError::Database);
    }
    let mut facts: Vec<_> = definitions
        .into_iter()
        .flat_map(|(version, definition)| {
            [
                format!("definition:{definition}"),
                format!("definition-version:{version}"),
            ]
        })
        .collect();
    if let Some(proof) = analysis_proof {
        facts.push(proof.reference().to_owned());
    }
    Ok((message, facts))
}

async fn current_interpretation(
    txn: &mut Transaction<'_, Postgres>,
    session: &WebChatModelSession,
    configured_timezone: &str,
    expected: WebChatInterpretationContext<'_>,
) -> Result<Option<InterpretationContext>, WebChatError> {
    for lock in [MODEL_CONSENT_LOCK, OWNER_ONBOARDING_LOCK] {
        try_shared_lock(txn, lock)
            .await
            .map_err(|_| WebChatError::Database)?;
    }
    let timezone = owner_timezone(txn, configured_timezone)
        .await
        .map_err(|_| WebChatError::Database)?;
    if expected.model.kind() != DestinationKind::Model
        || timezone != session.timezone
        || !onboarding_allows_model(txn, ModelCategories::HEALTH_AND_DIARY).await?
    {
        return Ok(None);
    }
    let query = InterpretationContextQuery {
        binding: session.binding(),
        model: expected.model,
        now: session.at,
        clarification_clock: session.clarification_clock(),
        mentioned_events: expected.mentioned_events,
    };
    let Ok(context) = interpretation_context_in(txn, query).await else {
        return Ok(None);
    };
    if context.channel != super::DESTINATION
        || context.digest().map_err(|_| WebChatError::Database)? != expected.digest
    {
        return Ok(None);
    }
    Ok(Some(context))
}

async fn apply_model_mutation(
    txn: &mut Transaction<'_, Postgres>,
    session: &WebChatModelSession,
    receipt: &InboundReceipt,
    command: &AgentInterpretation,
    offered: &[serde_json::Value],
) -> Option<Vec<StoredSystemEvent>> {
    if command.intent() == AgentIntent::Log {
        return apply_log(txn, session, receipt, command).await;
    }
    let correction = AgentCorrection::parse(command).ok()?;
    correction
        .apply(
            txn,
            AgentCorrectionContext {
                owner_id: session.envelope.owner_id(),
                actor: super::DESTINATION,
                operation_id: receipt.operation_id,
                text: session.envelope.text()?.trim(),
                received_at: session.at,
                offered,
                timezone: &session.timezone,
            },
        )
        .await
        .ok()
        .map(|saved| vec![saved])
}

async fn apply_log(
    txn: &mut Transaction<'_, Postgres>,
    session: &WebChatModelSession,
    receipt: &InboundReceipt,
    command: &AgentInterpretation,
) -> Option<Vec<StoredSystemEvent>> {
    let text = session.envelope.text()?.trim();
    if command.intent() != AgentIntent::Log
        || command.confidence() < 0.85
        || command.target_event_id().is_some()
        || command.target_question_id().is_some()
        || !command.question_context_ids().is_empty()
        || command.events().iter().any(|event| {
            event.payload().kind() == "medication"
                || ["reason_event_id", "episode_id", "activity_id"]
                    .iter()
                    .any(|key| !event.payload().json()[key].is_null())
        })
    {
        return None;
    }
    command.verify_time_evidence(text, session.at, None).ok()?;
    Database::require_diary_owner(txn, session.envelope.owner_id())
        .await
        .ok()?;
    let key = format!("web-chat:{}", session.envelope.external_event_id());
    Database::create_agent_log_batch_in(
        txn,
        CreateAgentLogBatch {
            owner_id: session.envelope.owner_id(),
            command,
            authenticated_actor: super::DESTINATION,
            message_text: text,
            source: EventSource::Manual,
            operation_id: receipt.operation_id,
            idempotency_prefix: &key,
        },
    )
    .await
    .ok()
}

async fn existing_receipt(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
) -> Result<Option<InboundReceipt>, WebChatError> {
    let existing: Option<(Uuid, Uuid, Option<String>)> = sqlx::query_as(
        "SELECT id,operation_id,normalized_text FROM inbound_messages
         WHERE owner_id=$1 AND conversation_id=$2 AND channel='web' AND channel_instance_id='local'
           AND external_event_id=$3 AND revision=$4",
    )
    .bind(envelope.owner_id())
    .bind(envelope.conversation_id())
    .bind(envelope.external_event_id())
    .bind(envelope.revision())
    .fetch_optional(&mut **txn)
    .await?;
    if let Some((message_id, operation_id, text)) = existing {
        if text.as_deref() != envelope.text() {
            return Err(WebChatError::ReusedMessage);
        }
        return Ok(Some(InboundReceipt {
            message_id,
            operation_id,
            supersedes_id: None,
            created: false,
        }));
    }
    Ok(None)
}
