pub mod actions;
mod clarification;
pub use clarification::{ClarificationClock, ClarificationError, ClarificationReply};
pub(crate) use clarification::{
    cancel_in, clarification_active_at, clear_history_selection, clear_selected_record,
    lock as lock_clarification, pending_in as pending_clarification_in, record_history_selection,
    save_in as save_clarification_in,
};

mod agent_batch;
pub(crate) mod agent_correction;
pub use agent_batch::CreateAgentLogBatch;

pub mod custom_entries;
pub mod custom_registry;
mod definition_discovery;
pub use custom_registry::{
    ActivatedCustomDefinition, CustomRegistryError, ManageCustomDefinition, StoredCustomDefinition,
};
pub use definition_discovery::DefinitionPage;

mod metrics;
mod proactive_answers;
pub(crate) use proactive_answers::caffeine_answer_at;
pub(crate) use proactive_answers::reconcile as reconcile_answers;
mod reads;
mod revisions;
pub(crate) use revisions::{corrected_report, lock_update_target, update_system_event_in};
pub(crate) mod undo;
mod wearable;
mod wellbeing;

pub(crate) use proactive_answers::context as question_context;
pub(crate) use reads::interpretation_context_in;
pub use reads::{
    DiaryEventRange, DiaryReadError, InterpretationEvents, InterpretationEventsQuery,
    ModelDiaryPage, ModelDiaryQuery, event_document,
};
pub use reads::{InterpretationContext, InterpretationContextQuery};
pub(crate) use reads::{analysis_events_in, caffeine_total, model_events_in, owner_events_in};
pub use revisions::{DeleteEvent, UpdateSystemEvent};
pub use undo::{UndoDiaryOperation, UndoneDiaryOperation};
pub use wearable::WearableError;

use std::{error::Error, fmt};

use ga_domain::{
    event_input::{EventInput, InvalidEvent},
    event_provenance::InvalidClockUncertainty,
    event_replay::{ReplayConflict, check_idempotency_key},
    event_schema::CustomEntryError,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness,
    system_definitions::{DefinitionStoreError, ensure_system_definition_in},
};

const MUTATION_LOCK: i64 = 72104619;

pub struct CreateSystemEvent<'a> {
    pub owner_id: Uuid,
    pub authenticated_actor: &'a str,
    pub report: &'a EventInput,
    pub idempotency_key: Option<&'a str>,
    pub operation_id: Option<Uuid>,
    pub clock_uncertainty_seconds: Option<u32>,
}

pub struct StoredSystemEvent {
    pub id: Uuid,
    pub revision: i32,
    pub reused: bool,
    pub snapshot: Value,
}

#[derive(Debug)]
pub enum DiaryWriteError {
    Storage(ReadinessError),
    Cancelled,
    UnknownOwner,
    Input(InvalidEvent),
    InvalidBatch,
    TrackingDisabled,
    EventMissing,
    RevisionChanged,
    DeletionForbidden,
    UnsupportedCorrection,
    NothingToUndo,
    UndoConflict,
    CustomEntry,
    CustomInput(CustomEntryError),
    OperationForbidden,
    SelfReference(&'static str),
    LinkedObservations,
    SymptomBounds,
    InvalidRelation,
    InvalidMetricMapping,
    InvalidRecordedState,
    Busy,
    Clock(InvalidClockUncertainty),
    Replay(ReplayConflict),
    Definition(DefinitionStoreError),
    Database,
}

impl fmt::Display for DiaryWriteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Storage(_) => "Diary storage is unavailable",
            Self::Cancelled => "Diary write was cancelled before commit",
            Self::UnknownOwner => "Diary owner could not be verified",
            Self::Input(error) => return error.fmt(formatter),
            Self::InvalidBatch => "Diary log batch is invalid",
            Self::TrackingDisabled => "The scenario pack is disabled",
            Self::EventMissing => "Event not found",
            Self::RevisionChanged => "Event changed; reload before editing",
            Self::DeletionForbidden => "Definition does not allow deletion",
            Self::UnsupportedCorrection => "The diary correction contains an unsupported change",
            Self::NothingToUndo => "Nothing to undo",
            Self::UndoConflict => "The last operation has already been changed or undone",
            Self::CustomEntry => "Custom entries must use the custom correction endpoint",
            Self::CustomInput(error) => return error.fmt(formatter),
            Self::OperationForbidden => "The recorded definition does not allow this operation",
            Self::SelfReference(message) => message,
            Self::LinkedObservations => {
                "Detach linked medication or symptom observations before changing this migraine"
            }
            Self::SymptomBounds => "Episode bounds would strand linked symptom observations",
            Self::InvalidRelation => "Diary report references an unavailable or incompatible entry",
            Self::InvalidMetricMapping => "Diary metric is outside its recorded contract",
            Self::InvalidRecordedState => "Recorded diary entry could not be interpreted",
            Self::Busy => "Diary writes are busy",
            Self::Clock(error) => return error.fmt(formatter),
            Self::Replay(error) => return error.fmt(formatter),
            Self::Definition(_) => "System diary definition is unavailable",
            Self::Database => "Diary write failed",
        })
    }
}

impl Error for DiaryWriteError {}

impl From<sqlx::Error> for DiaryWriteError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Create a validated built-in report and its audit and metric facts together.
    pub async fn create_system_event(
        &self,
        request: CreateSystemEvent<'_>,
    ) -> Result<StoredSystemEvent, DiaryWriteError> {
        self.create_system_event_unless_cancelled(request, || false)
            .await
    }

    /// Keep the entry, its audit, and projected metrics provisional until the
    /// caller confirms the request is still active at the commit boundary.
    pub async fn create_system_event_unless_cancelled(
        &self,
        request: CreateSystemEvent<'_>,
        is_cancelled: impl Fn() -> bool,
    ) -> Result<StoredSystemEvent, DiaryWriteError> {
        if is_cancelled() {
            return Err(DiaryWriteError::Cancelled);
        }
        request
            .report
            .validate_for_write()
            .map_err(DiaryWriteError::Input)?;
        let mut txn = self.begin_diary_write(request.owner_id).await?;
        let written = Self::create_system_event_in(&mut txn, request).await?;
        commit_diary_write(txn, is_cancelled).await?;
        Ok(written)
    }

    pub(crate) async fn create_system_event_in(
        txn: &mut Transaction<'_, Postgres>,
        request: CreateSystemEvent<'_>,
    ) -> Result<StoredSystemEvent, DiaryWriteError> {
        if let Some(key) = request.idempotency_key {
            check_idempotency_key(key).map_err(DiaryWriteError::Replay)?;
            if let Some(existing) = existing_replay(txn, key, request.report).await? {
                return Ok(existing);
            }
        }

        let report = request.report;
        let kind = report.payload().kind();
        check_pack(
            txn,
            request.owner_id,
            kind,
            request.authenticated_actor.starts_with("wearable:"),
        )
        .await?;
        let version = ensure_system_definition_in(txn, kind)
            .await
            .map_err(DiaryWriteError::Definition)?;
        validate_relation(txn, report).await?;
        let now: OffsetDateTime = sqlx::query_scalar("SELECT now()")
            .fetch_one(&mut **txn)
            .await?;
        let clock = stored_instant(now)?;
        let provenance = report
            .provenance(
                Some(request.authenticated_actor),
                version.topology,
                clock,
                request.clock_uncertainty_seconds,
            )
            .map_err(DiaryWriteError::Clock)?;
        let id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO events (id, definition_version_id, kind, start, \"end\", timezone,
                 source, confidence, status, original_text, payload, topology, envelope_version,
                 time_precision, clock_uncertainty_seconds, assertion_kind, producer, transport,
                 author, evidence_refs, validation_status, recorded_at, ingested_at,
                 revision, deleted, idempotency_key)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, 1,
                 $13, $14, $15, $16, $17, $18, '[]'::jsonb, $19, $20, $20, 1, false, $21)",
        )
        .bind(id)
        .bind(version.id)
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
        .bind(
            provenance
                .clock_uncertainty_seconds
                .map(|value| value as i32),
        )
        .bind(provenance.assertion_kind)
        .bind(provenance.producer)
        .bind(provenance.transport)
        .bind(provenance.author)
        .bind(provenance.validation_status)
        .bind(now)
        .bind(request.idempotency_key)
        .execute(&mut **txn)
        .await?;
        let written = load_event(txn, id, false).await?;
        invalidate_migraine_insights(txn, kind).await?;
        append_audit(
            txn,
            AuditEntry {
                id,
                action: "create",
                before: None,
                after: &written.snapshot,
                actor: request.authenticated_actor,
                operation: request.operation_id,
                at: now,
            },
        )
        .await?;
        metrics::project_event(txn, id, &written.snapshot, now).await?;
        Ok(written)
    }

    pub(crate) async fn begin_diary_write(
        &self,
        owner: Uuid,
    ) -> Result<Transaction<'static, Postgres>, DiaryWriteError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(DiaryWriteError::Storage)?;
        Self::require_diary_owner(&mut txn, owner).await?;
        Ok(txn)
    }

    pub(crate) async fn require_diary_owner(
        txn: &mut Transaction<'_, Postgres>,
        owner: Uuid,
    ) -> Result<(), DiaryWriteError> {
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(MUTATION_LOCK)
            .fetch_one(&mut **txn)
            .await?;
        if !locked {
            return Err(DiaryWriteError::Busy);
        }
        let matches: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM people WHERE id = $1)")
                .bind(owner)
                .fetch_one(&mut **txn)
                .await?;
        if !matches {
            return Err(DiaryWriteError::UnknownOwner);
        }
        Ok(())
    }
}

async fn commit_diary_write(
    txn: Transaction<'_, Postgres>,
    is_cancelled: impl Fn() -> bool,
) -> Result<(), DiaryWriteError> {
    if is_cancelled() {
        txn.rollback().await?;
        return Err(DiaryWriteError::Cancelled);
    }
    txn.commit().await?;
    Ok(())
}

struct AuditEntry<'a> {
    id: Uuid,
    action: &'static str,
    before: Option<&'a Value>,
    after: &'a Value,
    actor: &'a str,
    operation: Option<Uuid>,
    at: OffsetDateTime,
}

async fn append_audit(
    txn: &mut Transaction<'_, Postgres>,
    audit: AuditEntry<'_>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO audit_log (event_id, action, before, after, actor, operation_id, created_at)
         VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7)",
    )
    .bind(audit.id)
    .bind(audit.action)
    .bind(audit.before.map(Value::to_string))
    .bind(audit.after.to_string())
    .bind(audit.actor)
    .bind(audit.operation)
    .bind(audit.at)
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn existing_replay(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    report: &EventInput,
) -> Result<Option<StoredSystemEvent>, DiaryWriteError> {
    let id: Option<Uuid> = sqlx::query_scalar("SELECT id FROM events WHERE idempotency_key = $1")
        .bind(key)
        .fetch_optional(&mut **txn)
        .await?;
    let Some(id) = id else {
        return Ok(None);
    };
    let after = creation_audit(txn, id).await?;
    report
        .verify_creation_replay(Some(&after))
        .map_err(DiaryWriteError::Replay)?;
    Ok(Some(load_event(txn, id, true).await?))
}

async fn creation_audit(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
) -> Result<Value, DiaryWriteError> {
    let unavailable = DiaryWriteError::Replay(ReplayConflict::AuditUnavailable);
    let data = sqlx::query_scalar::<_, Option<String>>(
        "SELECT after::text FROM audit_log WHERE event_id = $1 AND action = 'create' ORDER BY id LIMIT 1",
    ).bind(id).fetch_optional(&mut **txn).await?.flatten().ok_or(unavailable)?;
    serde_json::from_str(&data)
        .map_err(|_| DiaryWriteError::Replay(ReplayConflict::AuditUnavailable))
}

async fn check_pack(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    kind: &str,
    collection: bool,
) -> Result<(), DiaryWriteError> {
    let pack = match kind {
        "caffeine" | "caffeine_absence" | "caffeine_log_complete" => "caffeine",
        "migraine" | "medication" | "headache_observation" | "symptom_observation" => "migraine",
        "wellbeing_observation" => "wellbeing",
        "activity_effort" => "training",
        "nap" => "sleep",
        _ => "general_diary",
    };
    let admitted: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS (SELECT 1 FROM module_configs WHERE owner_id = $1)
             OR EXISTS (SELECT 1 FROM module_configs WHERE owner_id = $1 AND pack_key = $2
                 AND CASE WHEN $3 THEN collection_enabled ELSE tracking_enabled END)",
    )
    .bind(owner)
    .bind(pack)
    .bind(collection)
    .fetch_one(&mut **txn)
    .await?;
    if !admitted {
        return Err(DiaryWriteError::TrackingDisabled);
    }
    Ok(())
}

async fn validate_relation(
    txn: &mut Transaction<'_, Postgres>,
    report: &EventInput,
) -> Result<(), DiaryWriteError> {
    validate_stored_relation(
        txn,
        report.payload().kind(),
        report.payload().json(),
        report.start().into(),
    )
    .await
}

async fn validate_stored_relation(
    txn: &mut Transaction<'_, Postgres>,
    kind: &str,
    payload: &Value,
    start: OffsetDateTime,
) -> Result<(), DiaryWriteError> {
    let valid = match kind {
        "activity_effort" => {
            sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM activities WHERE id = $1 AND start <= $2)",
            )
            .bind(payload["activity_id"].as_str())
            .bind(start)
            .fetch_one(&mut **txn)
            .await?
        }
        "symptom_observation" => {
            let related = relation_id(&payload["episode_id"])?;
            sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM events WHERE id = $1 AND NOT deleted
                   AND kind = 'migraine' AND status = 'confirmed'
                   AND start <= $2 AND (\"end\" IS NULL OR \"end\" >= $2))",
            )
            .bind(related)
            .bind(start)
            .fetch_one(&mut **txn)
            .await?
        }
        "medication" if !payload["reason_event_id"].is_null() => {
            let related = relation_id(&payload["reason_event_id"])?;
            sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM events WHERE id = $1 AND NOT deleted AND kind = 'migraine')",
            )
            .bind(related)
            .fetch_one(&mut **txn)
            .await?
        }
        _ => true,
    };
    if !valid {
        return Err(DiaryWriteError::InvalidRelation);
    }
    Ok(())
}

fn relation_id(value: &Value) -> Result<Uuid, DiaryWriteError> {
    value
        .as_str()
        .and_then(|value| Uuid::parse_str(value).ok())
        .ok_or(DiaryWriteError::InvalidRelation)
}

type EventSnapshotRow = (
    String,
    OffsetDateTime,
    Option<OffsetDateTime>,
    OffsetDateTime,
    OffsetDateTime,
    OffsetDateTime,
    OffsetDateTime,
);

pub(crate) async fn load_event(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    reused: bool,
) -> Result<StoredSystemEvent, DiaryWriteError> {
    let row = sqlx::query_as::<_, EventSnapshotRow>(
        "SELECT to_jsonb(events)::text, start, \"end\", created_at, updated_at, recorded_at, ingested_at
         FROM events WHERE id = $1",
    )
    .bind(id)
    .fetch_one(&mut **txn)
    .await?;
    let snapshot = event_snapshot(row)?;
    let revision = snapshot["revision"]
        .as_i64()
        .and_then(|value| i32::try_from(value).ok())
        .ok_or(DiaryWriteError::InvalidRecordedState)?;
    Ok(StoredSystemEvent {
        id,
        revision,
        reused,
        snapshot,
    })
}

fn event_snapshot(
    (document, start, end, created, updated, recorded, ingested): EventSnapshotRow,
) -> Result<Value, DiaryWriteError> {
    let mut snapshot: Value =
        serde_json::from_str(&document).map_err(|_| DiaryWriteError::InvalidRecordedState)?;
    let object = snapshot
        .as_object_mut()
        .ok_or(DiaryWriteError::InvalidRecordedState)?;
    for (name, timestamp) in [
        ("start", Some(start)),
        ("end", end),
        ("created_at", Some(created)),
        ("updated_at", Some(updated)),
        ("recorded_at", Some(recorded)),
        ("ingested_at", Some(ingested)),
    ] {
        let value = timestamp
            .map(stored_instant)
            .transpose()?
            .map(|value| value.to_string());
        object.insert(name.to_owned(), json!(value));
    }
    Ok(snapshot)
}

fn stored_instant(value: OffsetDateTime) -> Result<StoredInstant, DiaryWriteError> {
    StoredInstant::try_from(value).map_err(|_| DiaryWriteError::InvalidRecordedState)
}

async fn invalidate_migraine_insights(
    txn: &mut Transaction<'_, Postgres>,
    kind: &str,
) -> Result<(), sqlx::Error> {
    if matches!(
        kind,
        "migraine" | "headache_observation" | "symptom_observation"
    ) {
        sqlx::query(
            "UPDATE insights SET status = 'superseded'
             WHERE (category IN ('migraine', 'migraine_comparison')
                 OR evidence->>'tool' = 'analysis_migraine_windows') AND status != 'superseded'",
        )
        .execute(&mut **txn)
        .await?;
    }
    Ok(())
}
