use std::{error::Error, fmt, sync::LazyLock};

use ga_domain::{event_contract::definition_hash, event_provenance::EventTopology};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness};

const REGISTRY_LOCK: i64 = 72104628;
static CATALOG: LazyLock<Result<Vec<CatalogEntry>, DefinitionStoreError>> = LazyLock::new(load);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DefinitionStoreError {
    Unavailable(ReadinessError),
    UnknownKind,
    InvalidCatalog,
    InvalidStoredVersion,
    Database,
}

impl fmt::Display for DefinitionStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Unavailable(_) => "Diary definition storage is unavailable",
            Self::UnknownKind => "Unknown system event kind",
            Self::InvalidCatalog => "Bundled diary definition could not be verified",
            Self::InvalidStoredVersion => "Active diary definition does not match its registry",
            Self::Database => "Diary definition database operation failed",
        })
    }
}
impl Error for DefinitionStoreError {}

impl From<sqlx::Error> for DefinitionStoreError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SystemDefinition {
    pub id: Uuid,
    pub version: i32,
    pub topology: EventTopology,
}

#[derive(Deserialize)]
struct CatalogEntry {
    kind: String,
    hash: String,
    contract: Value,
}

impl CatalogEntry {
    fn field(&self, key: &str) -> Result<&Value, DefinitionStoreError> {
        self.contract
            .get(key)
            .ok_or(DefinitionStoreError::InvalidCatalog)
    }

    fn text(&self, key: &str) -> Result<&str, DefinitionStoreError> {
        self.field(key)?
            .as_str()
            .ok_or(DefinitionStoreError::InvalidCatalog)
    }
}

fn load() -> Result<Vec<CatalogEntry>, DefinitionStoreError> {
    let records: Vec<CatalogEntry> =
        serde_json::from_str(include_str!("../contracts/system-events.json"))
            .map_err(|_| DefinitionStoreError::InvalidCatalog)?;
    let mut previous = "";
    for entry in &records {
        if entry.kind.as_str() <= previous
            || entry.text("key")? != format!("system.{}", entry.kind)
            || definition_hash(&entry.contract).map_err(|_| DefinitionStoreError::InvalidCatalog)?
                != entry.hash
        {
            return Err(DefinitionStoreError::InvalidCatalog);
        }
        previous = &entry.kind;
        topology(entry.text("topology")?)?;
    }
    Ok(records)
}

fn topology(label: &str) -> Result<EventTopology, DefinitionStoreError> {
    match label {
        "point" => Ok(EventTopology::Point),
        "open_interval" => Ok(EventTopology::OpenInterval),
        "bounded_interval" => Ok(EventTopology::BoundedInterval),
        "flexible" => Ok(EventTopology::Flexible),
        _ => Err(DefinitionStoreError::InvalidCatalog),
    }
}

impl Database {
    /// Initialize the built-in definitions in the existing versioned store.
    pub async fn install_system_definitions(
        &self,
    ) -> Result<Vec<(String, SystemDefinition)>, DefinitionStoreError> {
        let catalog = CATALOG.as_ref().map_err(|error| *error)?;
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(DefinitionStoreError::Unavailable)?;
        lock_registry(&mut txn).await?;
        let mut installed = Vec::with_capacity(catalog.len());
        for entry in catalog {
            installed.push((entry.kind.clone(), ensure(&mut txn, entry).await?));
        }
        txn.commit().await?;
        Ok(installed)
    }

    /// Ensure one definition before a system event transaction references it.
    pub async fn ensure_system_definition(
        &self,
        kind: &str,
    ) -> Result<SystemDefinition, DefinitionStoreError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(DefinitionStoreError::Unavailable)?;
        let result = ensure_system_definition_in(&mut txn, kind).await?;
        txn.commit().await?;
        Ok(result)
    }
}

pub(crate) async fn ensure_system_definition_in(
    txn: &mut Transaction<'_, Postgres>,
    kind: &str,
) -> Result<SystemDefinition, DefinitionStoreError> {
    let catalog = CATALOG.as_ref().map_err(|error| *error)?;
    let entry = catalog
        .binary_search_by(|record| record.kind.as_str().cmp(kind))
        .ok()
        .and_then(|index| catalog.get(index))
        .ok_or(DefinitionStoreError::UnknownKind)?;
    lock_registry(txn).await?;
    ensure(txn, entry).await
}

async fn lock_registry(txn: &mut Transaction<'_, Postgres>) -> Result<(), sqlx::Error> {
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(REGISTRY_LOCK)
        .execute(&mut **txn)
        .await?;
    Ok(())
}

async fn ensure(
    txn: &mut Transaction<'_, Postgres>,
    entry: &CatalogEntry,
) -> Result<SystemDefinition, DefinitionStoreError> {
    let key = entry.text("key")?;
    let existing = sqlx::query_as::<_, (Uuid, String, Option<i32>)>(
        "SELECT id, namespace, current_version FROM event_definitions WHERE key = $1 FOR UPDATE",
    )
    .bind(key)
    .fetch_optional(&mut **txn)
    .await?;
    let (definition, current) = match existing {
        Some((id, namespace, current)) if namespace == "system" => (id, current),
        Some(_) => return Err(DefinitionStoreError::InvalidStoredVersion),
        None => {
            let id = Uuid::new_v4();
            sqlx::query("INSERT INTO event_definitions (id, namespace, key, status, revision, current_version)
                 VALUES ($1, 'system', $2, 'active', 1, 1)")
                .bind(id).bind(key).execute(&mut **txn).await?;
            (id, None)
        }
    };
    let mut had_version = false;
    if let Some(version) = current {
        let stored = sqlx::query_as::<_, (Uuid, String, String)>(
            "SELECT id, schema_hash, topology FROM event_definition_versions WHERE definition_id = $1 AND version = $2",
        )
        .bind(definition)
        .bind(version)
        .fetch_optional(&mut **txn)
        .await?;
        if let Some((id, hash, label)) = stored {
            had_version = true;
            if hash == entry.hash {
                return Ok(SystemDefinition {
                    id,
                    version,
                    topology: topology(&label)
                        .map_err(|_| DefinitionStoreError::InvalidStoredVersion)?,
                });
            }
        }
    }
    let version: i32 = sqlx::query_scalar(
        "SELECT coalesce(max(version), 0) + 1 FROM event_definition_versions WHERE definition_id = $1",
    )
    .bind(definition)
    .fetch_one(&mut **txn)
    .await?;
    let label = entry.text("topology")?;
    let id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO event_definition_versions (id, definition_id, version, schema, schema_hash,
             topology, field_metadata, labels, privacy, allowed_operations)
         VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8::jsonb, $9, $10::jsonb)",
    )
    .bind(id)
    .bind(definition)
    .bind(version)
    .bind(entry.field("schema")?.to_string())
    .bind(&entry.hash)
    .bind(label)
    .bind(entry.field("fields")?.to_string())
    .bind(entry.field("labels")?.to_string())
    .bind(entry.text("privacy")?)
    .bind(entry.field("allowed_operations")?.to_string())
    .execute(&mut **txn)
    .await?;
    sqlx::query(
        "UPDATE event_definitions
         SET current_version = $2, revision = revision + $3, updated_at = now()
         WHERE id = $1 AND (current_version IS DISTINCT FROM $2 OR $3 != 0)",
    )
    .bind(definition)
    .bind(version)
    .bind(i32::from(had_version))
    .execute(&mut **txn)
    .await?;
    Ok(SystemDefinition {
        id,
        version,
        topology: topology(label)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_pinned_definition_matches_its_python_hash() -> Result<(), DefinitionStoreError> {
        let records = CATALOG.as_ref().map_err(|error| *error)?;
        assert_eq!(records.len(), 19);
        assert_eq!(records[0].kind, "activity_effort");
        assert_eq!(records[18].kind, "wellbeing_observation");
        Ok(())
    }
}
