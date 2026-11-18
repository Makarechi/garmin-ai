use std::time::Duration;

use ga_domain::{
    agent_interpretation::{AgentIntent, AgentInterpretation},
    channel::{InboundEnvelope, OutboundIntent},
    event_input::EventInput,
    model_consent::ModelCategories,
};
use serde_json::{Map, Value, json};

use time::OffsetDateTime;

use super::{
    TelegramDiaryCommitError, TelegramInboundLease, TelegramModelContext,
    diary::{complete_reply, report, text_envelope},
    model_publication::{queue, verify},
};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{
        DiaryWriteError, StoredSystemEvent, UpdateSystemEvent, cancel_in, corrected_report,
        lock_update_target, reconcile_answers, update_system_event_in,
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
        if !matches!(command.intent(), AgentIntent::Update | AgentIntent::Close)
            || command.confidence() < 0.85
            || command.events().len() != 1
            || command.target_question_id().is_some()
            || !command.draft_links().is_empty()
            || !command.question_context_ids().is_empty()
        {
            return Err(TelegramDiaryCommitError::ClarificationRequired);
        }
        let target = command
            .target_event_id()
            .ok_or(TelegramDiaryCommitError::ExistingContextRequired)?;
        let proposed = &command.events()[0];
        let patch = correction_fields(command, proposed)?;
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
            let offered = context
                .offered
                .iter()
                .find(|entry| entry["id"].as_str().and_then(|id| id.parse().ok()) == Some(target))
                .ok_or(TelegramDiaryCommitError::ExistingContextRequired)?;
            if offered["kind"] != proposed.payload().kind() {
                return Err(TelegramDiaryCommitError::ExistingContextRequired);
            }
            let current_zone = offered["timezone"]
                .as_str()
                .ok_or(TelegramDiaryCommitError::ExistingContextRequired)?;
            command
                .verify_time_evidence(text, received_at, Some(current_zone))
                .map_err(TelegramDiaryCommitError::TimeClarification)?;
            let (revision, namespace, allowed) = lock_update_target(&mut txn, target)
                .await
                .map_err(TelegramDiaryCommitError::Diary)?;
            if !allowed || namespace.as_deref() == Some("user") {
                return Err(TelegramDiaryCommitError::Diary(
                    DiaryWriteError::OperationForbidden,
                ));
            }
            if offered["revision"].as_i64() != Some(i64::from(revision)) {
                return Err(TelegramDiaryCommitError::ModelContextChanged);
            }
            let before = crate::diary::load_event(&mut txn, target, false)
                .await
                .map_err(TelegramDiaryCommitError::Diary)?;
            if command.intent() == AgentIntent::Close
                && (before.snapshot["topology"] != "open_interval"
                    || before.snapshot["status"] != "confirmed")
            {
                return Err(TelegramDiaryCommitError::ExistingContextRequired);
            }
            let (report, display) = corrected_report(&before.snapshot, patch)
                .map_err(TelegramDiaryCommitError::Diary)?;
            let edited = update_system_event_in(
                &mut txn,
                UpdateSystemEvent {
                    owner_id: envelope.owner_id(),
                    authenticated_actor: &format!("telegram:{}", envelope.sender_ref()),
                    event_id: target,
                    expected_revision: revision,
                    report: &report,
                    operation_id: Some(lease.operation_id()),
                },
                before,
                namespace.as_deref(),
                display,
            )
            .await
            .map_err(TelegramDiaryCommitError::Diary)?;
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            reconcile_answers(&mut txn, envelope.owner_id(), at, expected.timezone)
                .await
                .map_err(TelegramDiaryCommitError::Diary)?;
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

fn correction_fields(
    command: &AgentInterpretation,
    proposed: &EventInput,
) -> Result<Map<String, Value>, TelegramDiaryCommitError> {
    let mut patch = Map::new();
    let mut payload = Map::new();
    let display = proposed.display_times();
    let end = || {
        proposed
            .end()
            .map(|end| display[1].map_or_else(|| end.to_string(), str::to_owned))
    };
    for field in command.changed_fields() {
        match field.as_str() {
            "start" => {
                patch.insert(
                    field.clone(),
                    json!(display[0].map_or_else(|| proposed.start().to_string(), str::to_owned)),
                );
            }
            "end" => {
                patch.insert(field.clone(), json!(end()));
            }
            "timezone" => {
                patch.insert(field.clone(), json!(proposed.timezone()));
            }
            path => {
                let label = path
                    .strip_prefix("payload.")
                    .filter(|key| {
                        !matches!(
                            *key,
                            "type" | "episode_id" | "reason_event_id" | "activity_id"
                        )
                    })
                    .ok_or(TelegramDiaryCommitError::ExistingContextRequired)?;
                let field = proposed
                    .payload()
                    .json()
                    .get(label)
                    .ok_or(TelegramDiaryCommitError::ExistingContextRequired)?;
                payload.insert(label.to_owned(), field.clone());
            }
        }
    }
    if command.intent() == AgentIntent::Close {
        let end = end().ok_or(TelegramDiaryCommitError::ClarificationRequired)?;
        patch.insert("end".into(), json!(end));
    }
    if !payload.is_empty() {
        patch.insert("payload".into(), Value::Object(payload));
    }
    if patch.is_empty() {
        return Err(TelegramDiaryCommitError::ClarificationRequired);
    }
    Ok(patch)
}
