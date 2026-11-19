use std::time::Duration;

use ga_domain::{
    access::Scope,
    event_schema::TrackerSetupDraft,
    localization::catalog_text,
    tracker_nl::{ExtractionError, TrackerExtraction},
    tracker_selection::is_tracker_proposal,
};
use serde_json::{Value, json};
use uuid::Uuid;

use super::super::TrackerTextError;
use super::TrackerTextSession;
use crate::{
    Database,
    diary::actions::{FormSubmission, TrackerFormWrite},
    share_policy::{self, DestinationKind, ShareDestination},
};

impl TrackerTextSession {
    pub async fn apply(mut self, extraction: TrackerExtraction) -> Result<Value, TrackerTextError> {
        tokio::time::timeout(Duration::from_millis(2500), async {
            let result = self.apply_uncommitted(extraction).await?;
            self.txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| TrackerTextError::Database)?
    }

    pub(crate) async fn apply_uncommitted(
        &mut self,
        extraction: TrackerExtraction,
    ) -> Result<Value, TrackerTextError> {
        let raw = extraction.document();
        let intent = raw["intent"].as_str().ok_or(TrackerTextError::Invalid)?;
        let response = if raw["confidence"]
            .as_f64()
            .ok_or(TrackerTextError::Invalid)?
            < 0.85
        {
            clarify(
                raw["clarification"]
                    .as_str()
                    .filter(|text| !text.is_empty())
                    .unwrap_or("Please clarify the tracker and details."),
            )
        } else if is_tracker_proposal(&self.request.text).map_err(|_| TrackerTextError::Database)?
            && matches!(intent, "create_entry" | "update_entry")
        {
            clarify("This sounds like a tracker setup request, not a completed entry.")
        } else {
            match intent {
                "propose_tracker" => {
                    if !self.scopes.permits(&[Scope::ManageDefinitions]) {
                        return Err(TrackerTextError::Forbidden);
                    }
                    let draft = TrackerSetupDraft::from_json(&raw["tracker_draft"])
                        .map_err(|_| TrackerTextError::Invalid)?;
                    let preview = Database::preview_tracker_setup_in(&mut self.txn, &draft)
                        .await
                        .map_err(|_| TrackerTextError::Database)?;
                    json!({"schema_version":"tracker.nl.v1","intent":"tracker_proposal","preview":preview,"written":false})
                }
                "change_tracker" => {
                    if !self.scopes.permits(&[Scope::ManageDefinitions]) {
                        return Err(TrackerTextError::Forbidden);
                    }
                    let candidate = self
                        .candidates
                        .iter()
                        .find(|candidate| {
                            candidate["definition_version_id"] == raw["definition_version_id"]
                        })
                        .ok_or(TrackerTextError::Invalid)?;
                    let draft = TrackerSetupDraft::from_json(&raw["tracker_draft"])
                        .map_err(|_| TrackerTextError::Invalid)?;
                    if draft.document()["key"].as_str()
                        != candidate["definition_key"]
                            .as_str()
                            .and_then(|key| key.strip_prefix("user."))
                    {
                        return Err(TrackerTextError::Invalid);
                    }
                    json!({"schema_version":"tracker.nl.v1","intent":"definition_change_proposal","definition_version_id":candidate["definition_version_id"],"definition":draft.definition().contract(),"written":false})
                }
                "clarify" | "none" => {
                    json!({"schema_version":"tracker.nl.v1","intent":intent,"clarification":raw["clarification"],"written":false})
                }
                "create_entry" | "update_entry" => self.write(&extraction).await?,
                _ => return Err(TrackerTextError::Invalid),
            }
        };
        Ok(response)
    }

    async fn write(&mut self, extraction: &TrackerExtraction) -> Result<Value, TrackerTextError> {
        if !self.scopes.permits(&[Scope::ReadDiary, Scope::WriteDiary]) {
            return Err(TrackerTextError::Forbidden);
        }
        let raw = extraction.document();
        let candidate = self
            .candidates
            .iter()
            .find(|candidate| candidate["definition_version_id"] == raw["definition_version_id"])
            .ok_or(TrackerTextError::Invalid)?;
        let update = raw["intent"] == "update_entry";
        let version: Uuid = raw["definition_version_id"]
            .as_str()
            .and_then(|value| value.parse().ok())
            .ok_or(TrackerTextError::Invalid)?;
        Database::require_diary_owner(&mut self.txn, self.owner)
            .await
            .map_err(|_| TrackerTextError::Database)?;
        let action = if update {
            let selected = self.selected.as_ref().ok_or(TrackerTextError::Invalid)?;
            if raw["event_id"] != selected["id"]
                || raw["definition_version_id"] != selected["definition_version_id"]
            {
                return Err(TrackerTextError::Invalid);
            }
            format!(
                "edit:{}:{}",
                selected["id"].as_str().ok_or(TrackerTextError::Invalid)?,
                selected["revision"]
            )
        } else {
            format!("create:{version}")
        };
        let form =
            Database::tracker_generated_form_in(&mut self.txn, self.owner, &action, &self.locale)
                .await
                .map_err(|_| TrackerTextError::Invalid)?;
        let submission = match extraction.validated_submission(
            &self.request.text,
            candidate,
            &form,
            &self.timezone,
            self.message_at,
        ) {
            Ok(submission) => submission,
            Err(ExtractionError::Field) => {
                return Ok(clarify(
                    catalog_text(&self.locale, "tracker.value_clarification")
                        .ok_or(TrackerTextError::Database)?,
                ));
            }
            Err(ExtractionError::Fact) => {
                return Ok(clarify(
                    catalog_text(&self.locale, "tracker.fact_context_clarification")
                        .ok_or(TrackerTextError::Database)?,
                ));
            }
            Err(ExtractionError::Temporal { field, evidence }) => {
                let key = match evidence["reason"].as_str() {
                    Some("repeated_wall_time") => "agent.ambiguous_time",
                    Some("clock_offset_mismatch") => "agent.timezone_mismatch",
                    _ => "tracker.time_clarification",
                };
                let mut result =
                    clarify(catalog_text(&self.locale, key).ok_or(TrackerTextError::Database)?);
                let mut temporal = evidence
                    .as_object()
                    .ok_or(TrackerTextError::Database)?
                    .clone();
                temporal.insert("field".into(), json!(field));
                result["temporal_evidence"] = json!(temporal);
                return Ok(result);
            }
            Err(ExtractionError::Contract) => return Err(TrackerTextError::Database),
            Err(ExtractionError::Invalid) => return Err(TrackerTextError::Invalid),
        };
        share_policy::try_shared_lock(&mut self.txn, share_policy::MODEL_CONSENT_LOCK)
            .await
            .map_err(|_| TrackerTextError::Database)?;
        let destination = ShareDestination::new(DestinationKind::Model, &self.destination)
            .map_err(|_| TrackerTextError::Database)?;
        let requirements = self.sharing_requirements()?;
        for requirement in requirements {
            if !share_policy::allows(&mut self.txn, requirement, destination)
                .await
                .map_err(|_| TrackerTextError::Database)?
            {
                let mut fallback = self.fallback.clone();
                fallback["reason"] = json!("sensitive_tracker_consent_required");
                return Ok(fallback);
            }
        }
        let mut references = Vec::new();
        if update {
            references.extend(
                form["initial_evidence_refs"]
                    .as_array()
                    .ok_or(TrackerTextError::Database)?
                    .iter()
                    .filter(|reference| {
                        !reference.is_object() || reference.get("field_id").is_none()
                    })
                    .cloned(),
            );
        }
        references.extend(submission.evidence_refs.iter().cloned());
        let form = FormSubmission::parse(
            &serde_json::to_vec(&submission.document).map_err(|_| TrackerTextError::Invalid)?,
        )
        .map_err(TrackerTextError::Form)?;
        let idempotency = format!("nl:{}", self.request.operation_id);
        let saved = Database::submit_tracker_form_in(
            &mut self.txn,
            &action,
            &form,
            TrackerFormWrite {
                owner_id: self.owner,
                actor: &self.actor,
                source: Some(self.source),
                idempotency_key: (!update).then_some(idempotency.as_str()),
                original_text: Some(&self.request.text),
                evidence_refs: Some(&references),
            },
        )
        .await
        .map_err(TrackerTextError::Form)?;
        let result = json!({
            "schema_version": "tracker.nl.v1",
            "intent": raw["intent"],
            "event_id": saved.snapshot["id"],
            "revision": saved.snapshot["revision"],
            "definition_version_id": saved.snapshot["definition_version_id"],
            "evidence_refs": submission.evidence_refs,
            "written": true
        });
        let operation = self.request.operation_key(&self.actor);
        let hashes = self
            .request
            .request_hashes()
            .map_err(|_| TrackerTextError::Invalid)?;
        sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
            .bind(operation)
            .bind(json!({"request_hash":hashes[0],"result":result}).to_string())
            .execute(&mut *self.txn)
            .await?;
        Ok(result)
    }
}

fn clarify(text: &str) -> Value {
    json!({"schema_version":"tracker.nl.v1","intent":"clarify","clarification":text,"written":false})
}
