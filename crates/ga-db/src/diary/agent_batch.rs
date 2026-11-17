use ga_domain::{
    agent_interpretation::{AgentIntent, AgentInterpretation},
    event_input::EventSource,
};
use uuid::Uuid;

use super::{CreateSystemEvent, DiaryWriteError, StoredSystemEvent};
use crate::Database;

pub struct CreateAgentLogBatch<'a> {
    pub owner_id: Uuid,
    pub command: &'a AgentInterpretation,
    pub authenticated_actor: &'a str,
    pub message_text: &'a str,
    pub source: EventSource,
    pub operation_id: Uuid,
    pub idempotency_prefix: &'a str,
}

impl Database {
    /// Record one authenticated message as an atomic diary operation.
    pub async fn create_agent_log_batch(
        &self,
        request: CreateAgentLogBatch<'_>,
    ) -> Result<Vec<StoredSystemEvent>, DiaryWriteError> {
        if request.command.intent() != AgentIntent::Log || request.idempotency_prefix.is_empty() {
            return Err(DiaryWriteError::InvalidBatch);
        }
        let events = request.command.events();
        let links = request.command.draft_links();
        let mut parents = vec![None; events.len()];
        for link in links {
            parents[link.child_index] = Some(link.parent_index);
        }
        let order = (0..events.len())
            .filter(|index| parents[*index].is_none())
            .chain(links.iter().map(|link| link.child_index));
        let mut stored: Vec<Option<StoredSystemEvent>> = (0..events.len()).map(|_| None).collect();
        let mut txn = self.begin_diary_write(request.owner_id).await?;
        for index in order {
            let parent = parents[index]
                .map(|parent| {
                    stored[parent]
                        .as_ref()
                        .map(|row| row.id)
                        .ok_or(DiaryWriteError::InvalidBatch)
                })
                .transpose()?;
            let report = events[index]
                .for_agent_report(request.source, request.message_text, parent)
                .map_err(DiaryWriteError::Input)?;
            let key = format!("{}:{index}", request.idempotency_prefix);
            let entry = Self::create_system_event_in(
                &mut txn,
                CreateSystemEvent {
                    owner_id: request.owner_id,
                    authenticated_actor: request.authenticated_actor,
                    report: &report,
                    idempotency_key: Some(&key),
                    operation_id: Some(request.operation_id),
                    clock_uncertainty_seconds: None,
                },
            )
            .await?;
            stored[index] = Some(entry);
        }
        let entries = stored
            .into_iter()
            .collect::<Option<Vec<_>>>()
            .ok_or(DiaryWriteError::InvalidBatch)?;
        txn.commit().await?;
        Ok(entries)
    }
}
