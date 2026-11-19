use ga_domain::{
    agent_interpretation::{AgentIntent, AgentInterpretation, TimeClarification},
    event_input::EventInput,
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    DiaryWriteError, StoredSystemEvent, UpdateSystemEvent, corrected_report, load_event,
    lock_update_target, reconcile_answers, update_system_event_in,
};
use crate::Database;

pub(crate) enum AgentCorrectionError {
    ClarificationRequired,
    ExistingContextRequired,
    ModelContextChanged,
    TimeClarification(TimeClarification),
    Diary(DiaryWriteError),
    Database,
}

pub(crate) struct AgentCorrection<'a> {
    command: &'a AgentInterpretation,
    target: Uuid,
    patch: Map<String, Value>,
}

pub(crate) struct AgentCorrectionContext<'a> {
    pub owner_id: Uuid,
    pub actor: &'a str,
    pub operation_id: Uuid,
    pub text: &'a str,
    pub received_at: StoredInstant,
    pub offered: &'a [Value],
    pub timezone: &'a str,
}

impl<'a> AgentCorrection<'a> {
    pub fn parse(command: &'a AgentInterpretation) -> Result<Self, AgentCorrectionError> {
        if !matches!(command.intent(), AgentIntent::Update | AgentIntent::Close)
            || command.confidence() < 0.85
            || command.events().len() != 1
            || command.target_question_id().is_some()
            || !command.draft_links().is_empty()
            || !command.question_context_ids().is_empty()
        {
            return Err(AgentCorrectionError::ClarificationRequired);
        }
        let target = command
            .target_event_id()
            .ok_or(AgentCorrectionError::ExistingContextRequired)?;
        let patch = correction_fields(command, &command.events()[0])?;
        Ok(Self {
            command,
            target,
            patch,
        })
    }

    pub async fn apply(
        self,
        txn: &mut Transaction<'_, Postgres>,
        context: AgentCorrectionContext<'_>,
    ) -> Result<StoredSystemEvent, AgentCorrectionError> {
        let offered = context
            .offered
            .iter()
            .find(|entry| entry["id"].as_str().and_then(|id| id.parse().ok()) == Some(self.target))
            .ok_or(AgentCorrectionError::ExistingContextRequired)?;
        if offered["kind"] != self.command.events()[0].payload().kind() {
            return Err(AgentCorrectionError::ExistingContextRequired);
        }
        let timezone = offered["timezone"]
            .as_str()
            .ok_or(AgentCorrectionError::ExistingContextRequired)?;
        self.command
            .verify_time_evidence(context.text, context.received_at, Some(timezone))
            .map_err(AgentCorrectionError::TimeClarification)?;
        Database::require_diary_owner(txn, context.owner_id)
            .await
            .map_err(AgentCorrectionError::Diary)?;
        let (revision, namespace, allowed) = lock_update_target(txn, self.target)
            .await
            .map_err(AgentCorrectionError::Diary)?;
        if !allowed || namespace.as_deref() == Some("user") {
            return Err(AgentCorrectionError::Diary(
                DiaryWriteError::OperationForbidden,
            ));
        }
        if offered["revision"].as_i64() != Some(i64::from(revision)) {
            return Err(AgentCorrectionError::ModelContextChanged);
        }
        let before = load_event(txn, self.target, false)
            .await
            .map_err(AgentCorrectionError::Diary)?;
        if self.command.intent() == AgentIntent::Close
            && (before.snapshot["topology"] != "open_interval"
                || before.snapshot["status"] != "confirmed")
        {
            return Err(AgentCorrectionError::ExistingContextRequired);
        }
        let (report, display) =
            corrected_report(&before.snapshot, self.patch).map_err(AgentCorrectionError::Diary)?;
        let edited = update_system_event_in(
            txn,
            UpdateSystemEvent {
                owner_id: context.owner_id,
                authenticated_actor: context.actor,
                event_id: self.target,
                expected_revision: revision,
                report: &report,
                operation_id: Some(context.operation_id),
            },
            before,
            namespace.as_deref(),
            display,
        )
        .await
        .map_err(AgentCorrectionError::Diary)?;
        let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut **txn)
            .await
            .map_err(|_| AgentCorrectionError::Database)?;
        reconcile_answers(txn, context.owner_id, at, context.timezone)
            .await
            .map_err(AgentCorrectionError::Diary)?;
        Ok(edited)
    }
}

fn correction_fields(
    command: &AgentInterpretation,
    proposed: &EventInput,
) -> Result<Map<String, Value>, AgentCorrectionError> {
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
                    .ok_or(AgentCorrectionError::ExistingContextRequired)?;
                let field = proposed
                    .payload()
                    .json()
                    .get(label)
                    .ok_or(AgentCorrectionError::ExistingContextRequired)?;
                payload.insert(label.to_owned(), field.clone());
            }
        }
    }
    if command.intent() == AgentIntent::Close {
        let end = end().ok_or(AgentCorrectionError::ClarificationRequired)?;
        patch.insert("end".into(), json!(end));
    }
    if !payload.is_empty() {
        patch.insert("payload".into(), Value::Object(payload));
    }
    if patch.is_empty() {
        return Err(AgentCorrectionError::ClarificationRequired);
    }
    Ok(patch)
}
