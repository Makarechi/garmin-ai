use ga_domain::{
    event_input::{EventInput, ValidationMode},
    event_provenance::{EventTopology, restore_legacy_provenance},
    event_schema::validate_entry_values,
};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    AuditEntry, DiaryWriteError, append_audit, invalidate_migraine_insights, load_event, metrics,
    relation_id,
    revisions::{
        ensure_unreferenced, snapshot_instant, snapshot_kind, snapshot_text,
        sync_migraine_questions, tombstone, validate_symptom_bounds,
    },
    stored_instant, validate_stored_relation,
};
use crate::{Database, system_definitions::ensure_system_definition_in};

pub struct UndoDiaryOperation<'a> {
    pub owner_id: Uuid,
    pub authenticated_actor: &'a str,
}

pub struct UndoneDiaryOperation {
    pub id: Uuid,
    pub revision: i32,
    pub deleted: bool,
    pub count: usize,
}

type RecordedAudit = (Uuid, Option<Uuid>, Option<String>, Option<String>);

impl Database {
    /// Undo the actor's latest operation as one transaction, newest audit first.
    pub async fn undo_diary_operation(
        &self,
        request: UndoDiaryOperation<'_>,
    ) -> Result<UndoneDiaryOperation, DiaryWriteError> {
        let mut txn = self.begin_diary_write(request.owner_id).await?;
        let audits = sqlx::query_as::<_, RecordedAudit>(
            "WITH latest AS (
                 SELECT id, operation_id FROM audit_log WHERE actor = $1 AND action != 'undo'
                 ORDER BY id DESC LIMIT 1
             )
             SELECT entry.event_id, entry.operation_id, entry.before::text, entry.after::text
             FROM audit_log AS entry JOIN latest
                ON entry.id = latest.id OR entry.operation_id = latest.operation_id
             WHERE entry.actor = $1 AND entry.action != 'undo'
             ORDER BY entry.id DESC",
        )
        .bind(request.authenticated_actor)
        .fetch_all(&mut *txn)
        .await?;
        let count = audits.len();
        let mut first = None;
        for audit in audits {
            let changed = undo_audit(&mut txn, &request, audit, count).await?;
            first.get_or_insert(changed);
        }
        let first = first.ok_or(DiaryWriteError::NothingToUndo)?;
        txn.commit().await?;
        Ok(first)
    }
}

async fn undo_audit(
    txn: &mut Transaction<'_, Postgres>,
    request: &UndoDiaryOperation<'_>,
    (id, operation, before, after): RecordedAudit,
    count: usize,
) -> Result<UndoneDiaryOperation, DiaryWriteError> {
    let locked: Option<i32> =
        sqlx::query_scalar("SELECT revision FROM events WHERE id = $1 FOR UPDATE")
            .bind(id)
            .fetch_optional(&mut **txn)
            .await?;
    let audit_after = parse_audit(after)?;
    if locked.map(i64::from) != audit_after["revision"].as_i64() || locked.is_none() {
        return Err(DiaryWriteError::UndoConflict);
    }
    let current = load_event(txn, id, false).await?;
    let mut original = parse_audit(before)?;
    let current_kind = snapshot_kind(&current.snapshot)?;
    if original.is_null()
        || (current_kind == "migraine"
            && (original["kind"] != "migraine" || original["deleted"] == true))
    {
        ensure_unreferenced(txn, id, false).await?;
    } else if current_kind == "migraine" && original["status"] != "confirmed" {
        ensure_unreferenced(txn, id, true).await?;
    }
    let transition = if original.is_null() {
        tombstone(txn, id).await?
    } else {
        prepare_snapshot(txn, id, &current.snapshot, &mut original).await?;
        restore_snapshot(txn, id, &original).await?
    };
    let restored = load_event(txn, id, false).await?;
    let deleted = restored.snapshot["deleted"]
        .as_bool()
        .ok_or(DiaryWriteError::InvalidRecordedState)?;
    if !restored.snapshot["definition_version_id"].is_null() {
        metrics::invalidate(txn, id, transition).await?;
        if !deleted {
            metrics::project_event(txn, id, &restored.snapshot, transition).await?;
        }
    }
    invalidate_migraine_insights(txn, current_kind).await?;
    let kind = snapshot_kind(&restored.snapshot)?;
    if kind != current_kind {
        invalidate_migraine_insights(txn, kind).await?;
    }
    sync_migraine_questions(
        txn,
        request.owner_id,
        id,
        &current.snapshot,
        &restored.snapshot,
        transition,
    )
    .await?;
    append_audit(
        txn,
        AuditEntry {
            id,
            action: "undo",
            before: Some(&current.snapshot),
            after: &restored.snapshot,
            actor: request.authenticated_actor,
            operation,
            at: transition,
        },
    )
    .await?;
    Ok(UndoneDiaryOperation {
        id,
        revision: restored.revision,
        deleted,
        count,
    })
}

fn parse_audit(document: Option<String>) -> Result<Value, DiaryWriteError> {
    document
        .map(|document| serde_json::from_str(&document))
        .transpose()
        .map_err(|_| DiaryWriteError::InvalidRecordedState)
        .map(|document| document.unwrap_or(Value::Null))
}

type RecordedDefinition = (String, i32, i32, String);

async fn prepare_snapshot(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    current: &Value,
    original: &mut Value,
) -> Result<(), DiaryWriteError> {
    let kind = snapshot_kind(original)?.to_owned();
    let start =
        snapshot_instant(original, "start")?.ok_or(DiaryWriteError::InvalidRecordedState)?;
    let end = snapshot_instant(original, "end")?;
    let deleted = original["deleted"]
        .as_bool()
        .ok_or(DiaryWriteError::InvalidRecordedState)?;
    let version = if original["definition_version_id"].is_null() {
        None
    } else {
        Some(relation_id(&original["definition_version_id"])?)
    };
    if !deleted {
        let contract = match version {
            Some(version) => sqlx::query_as::<_, RecordedDefinition>(
                "SELECT definition.namespace, definition.current_version, version.version, version.schema::text
                 FROM event_definition_versions AS version
                 JOIN event_definitions AS definition ON definition.id = version.definition_id
                 WHERE version.id = $1",
            )
            .bind(version)
            .fetch_optional(&mut **txn)
            .await?,
            None => None,
        };
        validate_snapshot(original, contract)?;
        validate_stored_relation(txn, &kind, &original["payload"], start).await?;
        validate_symptom_bounds(txn, id, &kind, start, end).await?;
    }
    if version.is_none() {
        let version = ensure_system_definition_in(txn, &kind)
            .await
            .map_err(DiaryWriteError::Definition)?;
        original["definition_version_id"] = json!(version.id.to_string());
    }
    let topology = match original["topology"].as_str() {
        Some("point") => EventTopology::Point,
        Some("open_interval") => EventTopology::OpenInterval,
        Some("bounded_interval") => EventTopology::BoundedInterval,
        Some("flexible") => EventTopology::Flexible,
        None if end.is_none() && matches!(kind.as_str(), "migraine" | "illness") => {
            EventTopology::OpenInterval
        }
        None if end.is_none() || end == Some(start) => EventTopology::Point,
        None => EventTopology::BoundedInterval,
        Some(_) => return Err(DiaryWriteError::InvalidRecordedState),
    };
    original["topology"] = json!(topology.label());
    original["end"] = original.get("end").cloned().unwrap_or(Value::Null);
    if original.get("envelope_version").is_none() {
        restore_legacy_envelope(txn, id, current, original, topology).await?;
    }
    for column in ["recorded_at", "ingested_at"] {
        if original[column].is_null() {
            original[column] = current[column].clone();
        }
    }
    Ok(())
}

fn validate_snapshot(
    snapshot: &Value,
    contract: Option<RecordedDefinition>,
) -> Result<(), DiaryWriteError> {
    let mut historical = false;
    if let Some((namespace, current, version, schema)) = contract {
        let schema =
            serde_json::from_str(&schema).map_err(|_| DiaryWriteError::InvalidRecordedState)?;
        let mut payload = snapshot["payload"].clone();
        if namespace == "user" {
            payload
                .as_object_mut()
                .ok_or(DiaryWriteError::InvalidRecordedState)?
                .remove("type");
        }
        validate_entry_values(&schema, &payload)
            .map_err(|_| DiaryWriteError::InvalidRecordedState)?;
        if namespace == "user" {
            return Ok(());
        }
        historical = current != version;
    }
    let fields: Map<String, Value> = [
        "start",
        "end",
        "timezone",
        "source",
        "confidence",
        "status",
        "original_text",
        "payload",
    ]
    .into_iter()
    .filter_map(|key| {
        snapshot
            .get(key)
            .map(|value| (key.to_owned(), value.clone()))
    })
    .collect();
    let encoded = Value::Object(fields).to_string();
    match EventInput::from_json(
        encoded.as_bytes(),
        encoded.len(),
        ValidationMode::AuditedSnapshot,
    ) {
        Ok(_) => Ok(()),
        Err(_) if historical => Ok(()),
        Err(error) => Err(DiaryWriteError::Input(error)),
    }
}

async fn restore_legacy_envelope(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    current: &Value,
    original: &mut Value,
    topology: EventTopology,
) -> Result<(), DiaryWriteError> {
    let actor: Option<String> = sqlx::query_scalar(
        "SELECT actor FROM audit_log WHERE event_id = $1 AND action = 'create' ORDER BY id LIMIT 1",
    )
    .bind(id)
    .fetch_optional(&mut **txn)
    .await?;
    let recorded = snapshot_instant(original, "created_at")?
        .or(snapshot_instant(current, "created_at")?)
        .ok_or(DiaryWriteError::InvalidRecordedState)?;
    let canonical = restore_legacy_provenance(
        actor
            .as_deref()
            .filter(|actor| !actor.is_empty())
            .unwrap_or("api"),
        snapshot_text(original, "source")?,
        snapshot_text(original, "status")?,
        topology,
        stored_instant(recorded)?,
    )
    .ok_or(DiaryWriteError::InvalidRecordedState)?;
    for (column, value) in [
        ("envelope_version", json!(1)),
        ("time_precision", json!(canonical.time_precision)),
        ("assertion_kind", json!(canonical.assertion_kind)),
        ("producer", json!(canonical.producer)),
        ("transport", json!(canonical.transport)),
        ("author", json!(canonical.author)),
        ("evidence_refs", json!([])),
        ("validation_status", json!(canonical.validation_status)),
        ("recorded_at", json!(canonical.recorded_at.to_string())),
        ("ingested_at", json!(canonical.ingested_at.to_string())),
    ] {
        original[column] = value;
    }
    Ok(())
}

async fn restore_snapshot(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    original: &Value,
) -> Result<OffsetDateTime, DiaryWriteError> {
    Ok(sqlx::query_scalar(
        "UPDATE events AS entry SET
             (kind, timezone, source, confidence, status, original_text, payload, deleted,
              envelope_version, time_precision, clock_uncertainty_seconds, assertion_kind, producer,
              transport, author, evidence_refs, validation_status, recorded_at, ingested_at,
              start, \"end\", topology, definition_version_id) =
             (SELECT kind, timezone, source, confidence, status, original_text, payload, deleted,
                     envelope_version, time_precision, clock_uncertainty_seconds, assertion_kind,
                     producer, transport, author, evidence_refs, validation_status, recorded_at,
                     ingested_at, start, \"end\", topology, definition_version_id
                FROM jsonb_populate_record(entry, $2::jsonb)),
             revision = revision + 1, updated_at = clock_timestamp()
         WHERE id = $1 RETURNING updated_at",
    )
    .bind(id)
    .bind(original.to_string())
    .fetch_one(&mut **txn)
    .await?)
}
