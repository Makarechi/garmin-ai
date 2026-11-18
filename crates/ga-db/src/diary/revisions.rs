use ga_domain::{
    event_input::{EventInput, EventStatus},
    timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    AuditEntry, DiaryWriteError, StoredSystemEvent, append_audit, check_pack, commit_diary_write,
    invalidate_migraine_insights, load_event, metrics, relation_id, stored_instant,
    validate_relation,
};
use crate::{Database, system_definitions::ensure_system_definition_in};

pub struct UpdateSystemEvent<'a> {
    pub owner_id: Uuid,
    pub authenticated_actor: &'a str,
    pub event_id: Uuid,
    pub expected_revision: i32,
    pub report: &'a EventInput,
    pub operation_id: Option<Uuid>,
}

pub struct DeleteEvent<'a> {
    pub owner_id: Uuid,
    pub authenticated_actor: &'a str,
    pub event_id: Uuid,
    pub expected_revision: i32,
}

impl Database {
    /// Tombstone the expected revision, preserving the original payload in its audit.
    pub async fn delete_event(
        &self,
        request: DeleteEvent<'_>,
    ) -> Result<StoredSystemEvent, DiaryWriteError> {
        self.delete_event_unless_cancelled(request, || false).await
    }

    /// Delete only if the caller stays active through the audit and commit boundary.
    pub async fn delete_event_unless_cancelled(
        &self,
        request: DeleteEvent<'_>,
        is_cancelled: impl Fn() -> bool,
    ) -> Result<StoredSystemEvent, DiaryWriteError> {
        if is_cancelled() {
            return Err(DiaryWriteError::Cancelled);
        }
        let mut txn = self.begin_diary_write(request.owner_id).await?;
        let (revision, deleted, deletion_allowed, query_allowed) =
            sqlx::query_as::<_, (i32, bool, bool, bool)>(
                "SELECT entry.revision, entry.deleted,
                     coalesce(version.allowed_operations ? 'delete', true),
                     entry.definition_version_id IS NULL
                        OR coalesce(version.allowed_operations ? 'query', false)
                 FROM events AS entry
                 LEFT JOIN event_definition_versions AS version ON version.id = entry.definition_version_id
                 WHERE entry.id = $1 FOR UPDATE OF entry",
            )
            .bind(request.event_id)
            .fetch_optional(&mut *txn)
            .await?
            .ok_or(DiaryWriteError::EventMissing)?;
        if deleted {
            return Err(DiaryWriteError::EventMissing);
        }
        if revision != request.expected_revision {
            return Err(DiaryWriteError::RevisionChanged);
        }
        if !deletion_allowed {
            return Err(DiaryWriteError::DeletionForbidden);
        }
        let id = request.event_id;
        let before = load_event(&mut txn, id, false).await?;
        ensure_unreferenced(&mut txn, id, false).await?;
        let transition = tombstone(&mut txn, id).await?;
        let mut deleted = load_event(&mut txn, id, false).await?;
        metrics::invalidate(&mut txn, id, transition).await?;
        invalidate_migraine_insights(&mut txn, snapshot_kind(&before.snapshot)?).await?;
        sync_migraine_questions(
            &mut txn,
            request.owner_id,
            id,
            &before.snapshot,
            &deleted.snapshot,
            transition,
        )
        .await?;
        append_audit(
            &mut txn,
            AuditEntry {
                id,
                action: "delete",
                before: Some(&before.snapshot),
                after: &deleted.snapshot,
                actor: request.authenticated_actor,
                operation: None,
                at: transition,
            },
        )
        .await?;
        if !query_allowed {
            deleted.snapshot = serde_json::json!({"id": id.to_string(), "revision": deleted.revision, "deleted": true});
        }
        commit_diary_write(txn, is_cancelled).await?;
        Ok(deleted)
    }

    /// Apply a correction to exactly the authenticated owner's expected revision.
    pub async fn update_system_event(
        &self,
        request: UpdateSystemEvent<'_>,
    ) -> Result<StoredSystemEvent, DiaryWriteError> {
        let report = request.report;
        report
            .validate_for_write()
            .map_err(DiaryWriteError::Input)?;
        let mut txn = self.begin_diary_write(request.owner_id).await?;
        let (revision, deleted, namespace) = sqlx::query_as::<_, (i32, bool, Option<String>)>(
            "SELECT entry.revision, entry.deleted, definition.namespace
             FROM events AS entry
             LEFT JOIN event_definition_versions AS version ON version.id = entry.definition_version_id
             LEFT JOIN event_definitions AS definition ON definition.id = version.definition_id
             WHERE entry.id = $1 FOR UPDATE OF entry",
        )
        .bind(request.event_id)
        .fetch_optional(&mut *txn)
        .await?
        .ok_or(DiaryWriteError::EventMissing)?;
        if deleted {
            return Err(DiaryWriteError::EventMissing);
        }
        if revision != request.expected_revision {
            return Err(DiaryWriteError::RevisionChanged);
        }
        let before = load_event(&mut txn, request.event_id, false).await?;
        let old_kind = before.snapshot["kind"]
            .as_str()
            .ok_or(DiaryWriteError::InvalidRecordedState)?;
        let kind = report.payload().kind();
        if old_kind != kind {
            check_pack(&mut txn, request.owner_id, kind, false).await?;
        }
        if namespace.as_deref() == Some("user") {
            return Err(DiaryWriteError::CustomEntry);
        }
        reject_self_reference(report, request.event_id)?;
        validate_relation(&mut txn, report).await?;
        check_episode_links(&mut txn, request.event_id, old_kind, report).await?;
        let definition = ensure_system_definition_in(&mut txn, kind)
            .await
            .map_err(DiaryWriteError::Definition)?;

        let recorded: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *txn)
            .await?;
        let provenance = report
            .provenance(
                Some(request.authenticated_actor),
                definition.topology,
                stored_instant(recorded)?,
                None,
            )
            .map_err(DiaryWriteError::Clock)?;
        let transition: OffsetDateTime = sqlx::query_scalar(
            "UPDATE events
             SET definition_version_id = $2, kind = $3, start = $4, \"end\" = $5, timezone = $6,
                 source = $7, confidence = $8, status = $9, original_text = $10,
                 payload = $11::jsonb, topology = $12, envelope_version = 1,
                 time_precision = $13, clock_uncertainty_seconds = NULL, assertion_kind = $14,
                 producer = $15, transport = $16, author = $17, evidence_refs = '[]'::jsonb,
                 validation_status = $18, recorded_at = $19, ingested_at = $19,
                 revision = revision + 1, updated_at = clock_timestamp()
             WHERE id = $1 RETURNING updated_at",
        )
        .bind(request.event_id)
        .bind(definition.id)
        .bind(kind)
        .bind(OffsetDateTime::from(report.start()))
        .bind(report.end().map(OffsetDateTime::from))
        .bind(report.timezone())
        .bind(report.source().label())
        .bind(report.confidence())
        .bind(report.status().label())
        .bind(report.original_text())
        .bind(report.payload().json().to_string())
        .bind(provenance.topology.label())
        .bind(provenance.time_precision)
        .bind(provenance.assertion_kind)
        .bind(provenance.producer)
        .bind(provenance.transport)
        .bind(provenance.author)
        .bind(provenance.validation_status)
        .bind(recorded)
        .fetch_one(&mut *txn)
        .await?;
        invalidate_migraine_insights(&mut txn, old_kind).await?;
        if old_kind != kind {
            invalidate_migraine_insights(&mut txn, kind).await?;
        }
        let corrected = load_event(&mut txn, request.event_id, false).await?;
        sync_migraine_questions(
            &mut txn,
            request.owner_id,
            request.event_id,
            &before.snapshot,
            &corrected.snapshot,
            transition,
        )
        .await?;
        append_audit(
            &mut txn,
            AuditEntry {
                id: request.event_id,
                action: "update",
                before: Some(&before.snapshot),
                after: &corrected.snapshot,
                actor: request.authenticated_actor,
                operation: request.operation_id,
                at: transition,
            },
        )
        .await?;
        metrics::invalidate(&mut txn, request.event_id, transition).await?;
        metrics::project_event(&mut txn, request.event_id, &corrected.snapshot, transition).await?;
        txn.commit().await?;
        Ok(corrected)
    }
}

fn reject_self_reference(report: &EventInput, event: Uuid) -> Result<(), DiaryWriteError> {
    let payload = report.payload().json();
    let (link, message) = match report.payload().kind() {
        "symptom_observation" => (
            &payload["episode_id"],
            "A symptom observation cannot reference itself",
        ),
        "medication" => (
            &payload["reason_event_id"],
            "A medication cannot reference itself",
        ),
        _ => return Ok(()),
    };
    if !link.is_null() && relation_id(link)? == event {
        return Err(DiaryWriteError::SelfReference(message));
    }
    Ok(())
}

async fn check_episode_links(
    txn: &mut Transaction<'_, Postgres>,
    event: Uuid,
    old_kind: &str,
    report: &EventInput,
) -> Result<(), DiaryWriteError> {
    let kind = report.payload().kind();
    if old_kind == "migraine" && (kind != "migraine" || report.status() != EventStatus::Confirmed) {
        ensure_unreferenced(txn, event, kind == "migraine").await?;
    }
    validate_symptom_bounds(
        txn,
        event,
        kind,
        report.start().into(),
        report.end().map(Into::into),
    )
    .await
}

pub(super) async fn validate_symptom_bounds(
    txn: &mut Transaction<'_, Postgres>,
    event: Uuid,
    kind: &str,
    start: OffsetDateTime,
    end: Option<OffsetDateTime>,
) -> Result<(), DiaryWriteError> {
    if kind == "migraine" {
        let outside: bool = sqlx::query_scalar(
            "SELECT EXISTS (
                SELECT 1 FROM events WHERE NOT deleted AND kind = 'symptom_observation'
                  AND payload->>'episode_id' = $1 AND (start < $2 OR start > $3)
             )",
        )
        .bind(event.to_string())
        .bind(start)
        .bind(end)
        .fetch_one(&mut **txn)
        .await?;
        if outside {
            return Err(DiaryWriteError::SymptomBounds);
        }
    }
    Ok(())
}

pub(super) async fn sync_migraine_questions(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    id: Uuid,
    before: &Value,
    after: &Value,
    now: OffsetDateTime,
) -> Result<(), DiaryWriteError> {
    let reminders: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS (SELECT 1 FROM module_configs WHERE owner_id = $1)
             OR EXISTS (SELECT 1 FROM module_configs WHERE owner_id = $1
               AND pack_key = 'migraine' AND reminders_enabled)",
    )
    .bind(owner)
    .fetch_one(&mut **txn)
    .await?;
    if !reminders
        || after["kind"] != "migraine"
        || after["status"] != "confirmed"
        || after["deleted"] == true
    {
        sqlx::query(
            "UPDATE pending_questions SET status = 'cancelled'
             WHERE kind = 'migraine' AND event_id = $1
               AND status IN ('pending','sending','sent','uncertain','acknowledged')",
        )
        .bind(id)
        .execute(&mut **txn)
        .await?;
    } else if snapshot_instant(after, "end")?.is_some_and(|end| end <= now) {
        sqlx::query(
            "UPDATE pending_questions SET status = 'answered',
                evidence = evidence || jsonb_build_object('answer_event_id', $2::text)
             WHERE kind = 'migraine' AND event_id = $1
               AND status IN ('pending','sending','sent','uncertain','acknowledged')",
        )
        .bind(id)
        .bind(id.to_string())
        .execute(&mut **txn)
        .await?;
    } else if !before["end"].is_null()
        || before["deleted"] == true
        || before["kind"] != "migraine"
        || before["status"] != "confirmed"
    {
        sqlx::query(
            "UPDATE pending_questions
             SET status = CASE WHEN sent_at IS NULL THEN 'pending' ELSE 'sent' END,
                 evidence = evidence - ARRAY['answer_event_id','answer_text','answered_at','acknowledged_events'],
                 earliest_send_at = CASE WHEN expires_at <= $2 THEN $2 ELSE earliest_send_at END,
                 expires_at = CASE WHEN expires_at <= $2 THEN $2 + interval '48 hours' ELSE expires_at END
             WHERE kind = 'migraine' AND event_id = $1 AND status IN ('answered','cancelled')",
        )
        .bind(id).bind(now).execute(&mut **txn).await?;
    }
    Ok(())
}

pub(super) async fn ensure_unreferenced(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    symptoms_only: bool,
) -> Result<(), DiaryWriteError> {
    let linked: bool = sqlx::query_scalar(
        "SELECT EXISTS (
            SELECT 1 FROM events WHERE NOT deleted
              AND ((kind = 'symptom_observation' AND payload->>'episode_id' = $1)
                OR (NOT $2 AND kind = 'medication' AND payload->>'reason_event_id' = $1))
         )",
    )
    .bind(id.to_string())
    .bind(symptoms_only)
    .fetch_one(&mut **txn)
    .await?;
    if linked {
        return Err(DiaryWriteError::LinkedObservations);
    }
    Ok(())
}

pub(super) async fn tombstone(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
) -> Result<OffsetDateTime, DiaryWriteError> {
    Ok(sqlx::query_scalar(
        "UPDATE events SET deleted = true, revision = revision + 1, updated_at = clock_timestamp()
         WHERE id = $1 RETURNING updated_at",
    )
    .bind(id)
    .fetch_one(&mut **txn)
    .await?)
}

pub(super) fn snapshot_kind(snapshot: &Value) -> Result<&str, DiaryWriteError> {
    snapshot_text(snapshot, "kind")
}

pub(super) fn snapshot_instant(
    snapshot: &Value,
    field: &str,
) -> Result<Option<OffsetDateTime>, DiaryWriteError> {
    match snapshot.get(field) {
        Some(Value::String(value)) => value
            .parse::<StoredInstant>()
            .map(OffsetDateTime::from)
            .map(Some)
            .map_err(|_| DiaryWriteError::InvalidRecordedState),
        None | Some(Value::Null) => Ok(None),
        _ => Err(DiaryWriteError::InvalidRecordedState),
    }
}

pub(super) fn snapshot_text<'a>(
    snapshot: &'a Value,
    field: &str,
) -> Result<&'a str, DiaryWriteError> {
    snapshot[field]
        .as_str()
        .ok_or(DiaryWriteError::InvalidRecordedState)
}
