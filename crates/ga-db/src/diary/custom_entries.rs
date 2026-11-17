use ga_domain::{
    event_replay::{ReplayConflict, check_idempotency_key},
    event_schema::{CustomEntry, ResolvedCustomEntry},
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    AuditEntry, DiaryWriteError, StoredSystemEvent, append_audit, creation_audit, load_event,
    metrics, stored_instant,
};
use crate::Database;

pub type StoredCustomEntry = StoredSystemEvent;

pub struct CreateCustomEntry<'a> {
    pub owner_id: Uuid,
    pub actor: &'a str,
    pub entry: &'a CustomEntry,
    pub idempotency_key: Option<&'a str>,
    pub evidence_refs: &'a [Value],
}

pub struct UpdateCustomEntry<'a> {
    pub owner_id: Uuid,
    pub actor: &'a str,
    pub id: Uuid,
    pub expected_revision: i32,
    pub entry: &'a CustomEntry,
    pub evidence_refs: &'a [Value],
}

impl Database {
    /// Create a tracker entry with the currently active definition and its mapped facts.
    pub async fn create_custom_entry(
        &self,
        request: CreateCustomEntry<'_>,
    ) -> Result<StoredCustomEntry, DiaryWriteError> {
        let mut txn = self.begin_diary_write(request.owner_id).await?;
        let timezone: String = sqlx::query_scalar("SELECT timezone FROM people WHERE id=$1")
            .bind(request.owner_id)
            .fetch_one(&mut *txn)
            .await?;
        if let Some(key) = request.idempotency_key {
            check_idempotency_key(key).map_err(DiaryWriteError::Replay)?;
            let row = sqlx::query_as::<_, (Uuid, Option<Uuid>)>(
                "SELECT id,definition_version_id FROM events WHERE idempotency_key=$1",
            )
            .bind(key)
            .fetch_optional(&mut *txn)
            .await?;
            if let Some((id, version)) = row {
                let version =
                    version.ok_or(DiaryWriteError::Replay(ReplayConflict::InputChanged))?;
                let contract = definition_version(
                    &mut txn,
                    request.owner_id,
                    request.entry.key(),
                    Some(version),
                )
                .await?
                .ok_or(DiaryWriteError::Replay(ReplayConflict::InputChanged))?;
                let entry = contract.resolve(request.entry, &timezone)?;
                let audit = creation_audit(&mut txn, id).await?;
                entry
                    .verify_creation(contract.id, &audit)
                    .map_err(DiaryWriteError::Replay)?;
                let stored = if contract.operations["query"] == true {
                    load_event(&mut txn, id, true).await?
                } else {
                    let revision = audit["revision"]
                        .as_i64()
                        .and_then(|value| i32::try_from(value).ok())
                        .ok_or(DiaryWriteError::InvalidRecordedState)?;
                    StoredCustomEntry {
                        id,
                        revision,
                        reused: true,
                        snapshot: audit,
                    }
                };
                txn.commit().await?;
                return Ok(stored);
            }
        }
        let contract = definition_version(&mut txn, request.owner_id, request.entry.key(), None)
            .await?
            .ok_or(DiaryWriteError::EventMissing)?;
        if contract.operations["create"] != true {
            return Err(DiaryWriteError::OperationForbidden);
        }
        let entry = contract.resolve(request.entry, &timezone)?;
        let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *txn)
            .await?;
        let document = fields(&entry, request.actor, request.evidence_refs, at)?;
        let id = Uuid::new_v4();
        sqlx::query(include_str!("custom_entries/insert.sql"))
            .bind(id)
            .bind(contract.id)
            .bind(document.to_string())
            .bind(request.idempotency_key)
            .execute(&mut *txn)
            .await?;
        let created = load_event(&mut txn, id, false).await?;
        append_audit(
            &mut txn,
            AuditEntry {
                id,
                action: "create",
                before: None,
                after: &created.snapshot,
                actor: request.actor,
                operation: None,
                at,
            },
        )
        .await?;
        metrics::project_event(&mut txn, id, &created.snapshot, at).await?;
        txn.commit().await?;
        Ok(created)
    }

    /// Edit the version originally recorded with this tracker entry.
    pub async fn update_custom_entry(
        &self,
        request: UpdateCustomEntry<'_>,
    ) -> Result<StoredCustomEntry, DiaryWriteError> {
        let mut txn = self.begin_diary_write(request.owner_id).await?;
        let (version, revision, timezone): (Uuid, i32, String) = sqlx::query_as(
            "SELECT definition_version_id,revision,timezone FROM events
             WHERE id=$1 AND NOT deleted AND definition_version_id IS NOT NULL FOR UPDATE",
        )
        .bind(request.id)
        .fetch_optional(&mut *txn)
        .await?
        .ok_or(DiaryWriteError::EventMissing)?;
        if revision != request.expected_revision {
            return Err(DiaryWriteError::RevisionChanged);
        }
        let contract = definition_version(
            &mut txn,
            request.owner_id,
            request.entry.key(),
            Some(version),
        )
        .await?
        .ok_or(DiaryWriteError::CustomEntry)?;
        if contract.operations["update"] != true {
            return Err(DiaryWriteError::OperationForbidden);
        }
        let entry = contract.resolve(request.entry, &timezone)?;
        let before = load_event(&mut txn, request.id, false).await?;
        let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *txn)
            .await?;
        let document = fields(&entry, request.actor, request.evidence_refs, at)?;
        sqlx::query(include_str!("custom_entries/update.sql"))
            .bind(request.id)
            .bind(document.to_string())
            .execute(&mut *txn)
            .await?;
        let written = load_event(&mut txn, request.id, false).await?;
        append_audit(
            &mut txn,
            AuditEntry {
                id: request.id,
                action: "update",
                before: Some(&before.snapshot),
                after: &written.snapshot,
                actor: request.actor,
                operation: None,
                at,
            },
        )
        .await?;
        metrics::invalidate(&mut txn, request.id, at).await?;
        metrics::project_event(&mut txn, request.id, &written.snapshot, at).await?;
        txn.commit().await?;
        Ok(written)
    }
}

struct EntryContract {
    id: Uuid,
    topology: String,
    schema: Value,
    metadata: Value,
    operations: Value,
}

impl EntryContract {
    fn resolve<'a>(
        &self,
        entry: &'a CustomEntry,
        timezone: &'a str,
    ) -> Result<ResolvedCustomEntry<'a>, DiaryWriteError> {
        entry
            .resolve(&self.schema, &self.metadata, &self.topology, timezone)
            .map_err(DiaryWriteError::CustomInput)
    }
}

async fn definition_version(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    key: &str,
    historical: Option<Uuid>,
) -> Result<Option<EntryContract>, DiaryWriteError> {
    type ContractRow = (Uuid, String, String, String, String);
    let result = sqlx::query_as::<_, ContractRow>(
        "SELECT version.id,version.topology,version.schema::text,version.field_metadata::text,
            jsonb_build_object('create',version.allowed_operations ? 'create',
                'update',version.allowed_operations ? 'update','query',version.allowed_operations ? 'query')::text
         FROM event_definitions AS definition
         JOIN event_definition_versions AS version ON version.definition_id=definition.id
         WHERE definition.owner_id=$1 AND definition.key=$2 AND definition.namespace='user'
           AND (($3::uuid IS NOT NULL AND version.id=$3)
             OR ($3::uuid IS NULL AND definition.status='active' AND definition.current_version=version.version))",
    ).bind(owner).bind(key).bind(historical).fetch_optional(&mut **txn).await?;
    result
        .map(|(id, topology, schema, metadata, operations)| {
            let parse = |document| {
                serde_json::from_str(document).map_err(|_| DiaryWriteError::InvalidRecordedState)
            };
            Ok(EntryContract {
                id,
                topology,
                schema: parse(&schema)?,
                metadata: parse(&metadata)?,
                operations: parse(&operations)?,
            })
        })
        .transpose()
}

fn fields(
    entry: &ResolvedCustomEntry<'_>,
    actor: &str,
    evidence: &[Value],
    clock: OffsetDateTime,
) -> Result<Value, DiaryWriteError> {
    let at = stored_instant(clock)?;
    let provenance = entry
        .provenance(actor, at)
        .map_err(DiaryWriteError::Clock)?;
    Ok(json!({
        "kind": entry.key,
        "start": entry.start.to_string(), "end": entry.end.map(|end| end.to_string()),
        "topology": entry.topology.label(), "timezone": entry.timezone,
        "source": entry.source, "status": entry.status, "confidence": entry.confidence,
        "original_text": entry.original_text, "payload": entry.payload,
        "recorded_at": at.to_string(), "time_precision": provenance.time_precision,
        "assertion_kind": provenance.assertion_kind, "producer": provenance.producer,
        "transport": provenance.transport, "author": provenance.author,
        "evidence_refs": evidence, "validation_status": provenance.validation_status,
    }))
}
