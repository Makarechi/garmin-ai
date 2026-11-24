use std::collections::BTreeMap;

use futures_util::TryStreamExt;
use ga_domain::{
    event_contract::definition_hash,
    event_schema::validate_entry_values,
    legacy_json::{self, Utf8Layout},
    sha256_hex,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{CATALOG, CatalogEntry, DefinitionStoreError, ensure, insert_version, lock_registry};

const BATCH: i64 = 500;

pub(crate) async fn initialize_system_events(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), DefinitionStoreError> {
    let catalog = CATALOG.as_ref().map_err(|error| *error)?;
    lock_registry(txn).await?;
    for entry in catalog {
        let current = ensure(txn, entry).await?;
        backfill(txn, entry, current.id).await?;
    }
    let unresolved: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT FROM events e LEFT JOIN event_definition_versions v
          ON v.id=e.definition_version_id WHERE v.id IS NULL)",
    )
    .fetch_one(&mut **txn)
    .await?;
    if unresolved {
        return Err(DefinitionStoreError::InvalidStoredVersion);
    }
    let hashes: BTreeMap<_, _> = catalog
        .iter()
        .map(|entry| (entry.kind.as_str(), entry.hash.as_str()))
        .collect();
    let bytes = serde_json::to_vec(&hashes).map_err(|_| DefinitionStoreError::InvalidCatalog)?;
    let encoded = legacy_json::encode(&bytes, Utf8Layout::Archive, 16_384)
        .map_err(|_| DefinitionStoreError::InvalidCatalog)?;
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('registry:system:contract_digest',$1::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=now()
         WHERE app_state.value IS DISTINCT FROM excluded.value",
    )
    .bind(json!({"hash":sha256_hex(&encoded)}).to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn backfill(
    txn: &mut Transaction<'_, Postgres>,
    current: &CatalogEntry,
    current_version: Uuid,
) -> Result<(), DefinitionStoreError> {
    let old = if current.kind == "symptom_observation" {
        Some(legacy_symptom_contract(current)?)
    } else {
        None
    };
    let mut after: Option<Uuid> = None;
    let mut old_version = None;
    loop {
        let previous = after;
        let mut matched = Vec::new();
        let mut historical = Vec::new();
        {
            let mut rows = sqlx::query_as::<_, (Uuid, String)>(
                "SELECT id,payload::text FROM events WHERE kind=$1 AND definition_version_id IS NULL
                 AND ($2::uuid IS NULL OR id > $2) ORDER BY id LIMIT $3 FOR UPDATE",
            )
            .bind(&current.kind)
            .bind(after)
            .bind(BATCH)
            .fetch(&mut **txn);
            while let Some((id, raw)) = rows.try_next().await? {
                after = Some(id);
                let payload: Value = serde_json::from_str(&raw)
                    .map_err(|_| DefinitionStoreError::InvalidStoredVersion)?;
                if validate_entry_values(current.field("schema")?, &payload).is_ok() {
                    matched.push(id);
                } else if let Some(old) = old.as_ref()
                    && validate_entry_values(old.field("schema")?, &payload).is_ok()
                {
                    historical.push(id);
                }
            }
        }
        if previous == after {
            break;
        }
        link(txn, &matched, current_version).await?;
        if !historical.is_empty() {
            let contract = old.as_ref().ok_or(DefinitionStoreError::InvalidCatalog)?;
            let id = if let Some(id) = old_version {
                id
            } else {
                let id = ensure_legacy_version(txn, contract).await?;
                old_version = Some(id);
                id
            };
            link(txn, &historical, id).await?;
        }
    }
    Ok(())
}

fn legacy_symptom_contract(current: &CatalogEntry) -> Result<CatalogEntry, DefinitionStoreError> {
    let mut contract = current.contract.clone();
    let alternatives = contract["schema"]["allOf"][0]["anyOf"]
        .as_array_mut()
        .ok_or(DefinitionStoreError::InvalidCatalog)?;
    for branch in alternatives {
        if let Some(impact) = branch
            .get_mut("properties")
            .and_then(|fields| fields.get_mut("impact"))
        {
            *impact = json!({"type":"string"});
        }
    }
    let hash = definition_hash(&contract).map_err(|_| DefinitionStoreError::InvalidCatalog)?;
    Ok(CatalogEntry {
        kind: current.kind.clone(),
        hash,
        contract,
    })
}

async fn ensure_legacy_version(
    txn: &mut Transaction<'_, Postgres>,
    entry: &CatalogEntry,
) -> Result<Uuid, DefinitionStoreError> {
    let key = entry.text("key")?;
    let definition: Uuid = sqlx::query_scalar("SELECT id FROM event_definitions WHERE key=$1")
        .bind(key)
        .fetch_one(&mut **txn)
        .await?;
    let existing = sqlx::query_scalar(
        "SELECT id FROM event_definition_versions WHERE definition_id=$1 AND schema_hash=$2
         ORDER BY version LIMIT 1",
    )
    .bind(definition)
    .bind(&entry.hash)
    .fetch_optional(&mut **txn)
    .await?;
    match existing {
        Some(id) => Ok(id),
        None => Ok(insert_version(txn, definition, entry).await?.id),
    }
}

async fn link(
    txn: &mut Transaction<'_, Postgres>,
    events: &[Uuid],
    version: Uuid,
) -> Result<(), DefinitionStoreError> {
    if !events.is_empty() {
        sqlx::query("UPDATE events SET definition_version_id=$2,updated_at=now() WHERE id=ANY($1)")
            .bind(events)
            .bind(version)
            .execute(&mut **txn)
            .await?;
    }
    Ok(())
}
