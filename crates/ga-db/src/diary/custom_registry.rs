mod tracker_metrics;

use std::{collections::BTreeMap, error::Error, fmt};

use ga_domain::event_schema::{CustomDefinition, CustomDefinitionError};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::DiaryWriteError;
use crate::Database;

#[derive(Clone, Copy)]
pub struct ManageCustomDefinition<'a> {
    pub owner_id: Uuid,
    pub authenticated_actor: &'a str,
    pub may_manage_definitions: bool,
}

pub struct StoredCustomDefinition {
    pub id: Uuid,
    pub key: String,
    pub status: String,
    pub revision: i32,
    pub current_version: Option<i32>,
}

pub struct ActivatedCustomDefinition {
    pub id: Uuid,
    pub version: i32,
    pub new_definition_revision: i32,
}

#[derive(Debug)]
pub enum CustomRegistryError {
    Forbidden,
    Missing,
    KeyTaken,
    KeyChanged,
    RevisionChanged,
    Retired,
    NoDraft,
    InvalidStoredContract,
    FieldIdentityChanged,
    Spec(CustomDefinitionError),
    Diary(DiaryWriteError),
    Metric,
    Database,
}

impl fmt::Display for CustomRegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Forbidden => "Definition management permission is required",
            Self::Missing => "Custom definition was not found",
            Self::KeyTaken => "Definition key already exists",
            Self::KeyChanged => "Definition key is immutable",
            Self::RevisionChanged => "Definition changed; reload before saving",
            Self::Retired => "Retired definitions cannot be revised",
            Self::NoDraft => "Definition has no proposed contract",
            Self::InvalidStoredContract => "Recorded definition contract is invalid",
            Self::FieldIdentityChanged => "Existing field identities are immutable",
            Self::Spec(error) => return error.fmt(f),
            Self::Diary(error) => return error.fmt(f),
            Self::Metric => "Tracker metric contracts could not be bound",
            Self::Database => "Custom definition could not be saved",
        })
    }
}
impl Error for CustomRegistryError {}

impl From<sqlx::Error> for CustomRegistryError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    pub async fn draft_custom_definition(
        &self,
        request: ManageCustomDefinition<'_>,
        contract: &CustomDefinition,
    ) -> Result<StoredCustomDefinition, CustomRegistryError> {
        let mut txn = self.begin_definition_management(request).await?;
        let definition = Self::draft_custom_definition_in(&mut txn, request, contract).await?;
        txn.commit().await?;
        Ok(definition)
    }

    pub(super) async fn draft_custom_definition_in(
        txn: &mut Transaction<'_, Postgres>,
        request: ManageCustomDefinition<'_>,
        contract: &CustomDefinition,
    ) -> Result<StoredCustomDefinition, CustomRegistryError> {
        let id = Uuid::new_v4();
        let draft = draft_document(contract, request.authenticated_actor);
        let row = sqlx::query_as::<_, DefinitionRow>(
            "INSERT INTO event_definitions (id, owner_id, namespace, key, status, revision, draft)
             VALUES ($1,$2,'user',$3,'draft',1,$4::jsonb)
             ON CONFLICT (key) DO NOTHING
             RETURNING id, key, status, revision, current_version",
        )
        .bind(id)
        .bind(request.owner_id)
        .bind(contract.key())
        .bind(draft.to_string())
        .fetch_optional(&mut **txn)
        .await?
        .ok_or(CustomRegistryError::KeyTaken)?;
        Ok(stored(row))
    }

    pub async fn propose_custom_definition(
        &self,
        request: ManageCustomDefinition<'_>,
        id: Uuid,
        revision: i32,
        contract: &CustomDefinition,
    ) -> Result<StoredCustomDefinition, CustomRegistryError> {
        let mut txn = self.begin_definition_management(request).await?;
        let (current, _) = lock_definition(&mut txn, request.owner_id, id, revision).await?;
        if current.status == "retired" {
            return Err(CustomRegistryError::Retired);
        }
        if current.key != contract.key() {
            return Err(CustomRegistryError::KeyChanged);
        }
        check_field_identities(&mut txn, id, contract).await?;
        let draft = draft_document(contract, request.authenticated_actor);
        let row = sqlx::query_as::<_, DefinitionRow>(
            "UPDATE event_definitions
             SET draft=$2::jsonb, status=CASE WHEN current_version IS NULL THEN 'proposed' ELSE status END,
                 revision=revision+1, updated_at=now()
             WHERE id=$1 RETURNING id, key, status, revision, current_version",
        )
        .bind(id).bind(draft.to_string()).fetch_one(&mut *txn).await?;
        txn.commit().await?;
        Ok(stored(row))
    }

    pub async fn activate_custom_definition(
        &self,
        request: ManageCustomDefinition<'_>,
        id: Uuid,
        revision: i32,
    ) -> Result<ActivatedCustomDefinition, CustomRegistryError> {
        let mut txn = self.begin_definition_management(request).await?;
        let version = Self::activate_custom_definition_in(&mut txn, request, id, revision).await?;
        txn.commit().await?;
        Ok(version)
    }

    pub(super) async fn activate_custom_definition_in(
        txn: &mut Transaction<'_, Postgres>,
        request: ManageCustomDefinition<'_>,
        id: Uuid,
        revision: i32,
    ) -> Result<ActivatedCustomDefinition, CustomRegistryError> {
        let (current, draft) = lock_definition(txn, request.owner_id, id, revision).await?;
        if !matches!(current.status.as_str(), "draft" | "proposed" | "active") {
            return Err(CustomRegistryError::NoDraft);
        }
        let decoded = draft
            .map(|draft| serde_json::from_str::<Value>(&draft))
            .transpose()
            .map_err(|_| CustomRegistryError::InvalidStoredContract)?
            .ok_or(CustomRegistryError::NoDraft)?;
        let mut decoded = decoded;
        decoded
            .as_object_mut()
            .ok_or(CustomRegistryError::InvalidStoredContract)?
            .remove("actor");
        let contract = CustomDefinition::from_json(&decoded).map_err(CustomRegistryError::Spec)?;
        if contract.key() != current.key {
            return Err(CustomRegistryError::InvalidStoredContract);
        }
        let number: i32 = sqlx::query_scalar(
            "SELECT coalesce(max(version), 0) + 1 FROM event_definition_versions WHERE definition_id = $1",
        ).bind(id).fetch_one(&mut **txn).await?;
        let version = Uuid::new_v4();
        let value = contract.contract();
        let mut metadata = value["fields"].clone();
        for field in metadata
            .as_object_mut()
            .ok_or(CustomRegistryError::InvalidStoredContract)?
            .values_mut()
        {
            if field["metric_semantics"].is_null() {
                field
                    .as_object_mut()
                    .ok_or(CustomRegistryError::InvalidStoredContract)?
                    .remove("metric_semantics");
            }
        }
        sqlx::query(
            "INSERT INTO event_definition_versions (id, definition_id, version, schema, schema_hash,
                 topology, field_metadata, labels, privacy, allowed_operations)
             VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7::jsonb,$8::jsonb,$9,$10::jsonb)",
        )
        .bind(version).bind(id).bind(number).bind(value["schema"].to_string()).bind(contract.hash())
        .bind(value["topology"].as_str()).bind(metadata.to_string()).bind(value["labels"].to_string())
        .bind(value["privacy"].as_str()).bind(value["allowed_operations"].to_string())
        .execute(&mut **txn).await?;

        let is_tracker: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM tracker_configs WHERE definition_id = $1 AND owner_id = $2)",
        ).bind(id).bind(request.owner_id).fetch_one(&mut **txn).await?;
        if is_tracker {
            tracker_metrics::bind_tracker_metrics(txn, request.owner_id, version, &contract)
                .await?;
        }
        let new_definition_revision = sqlx::query_scalar(
            "UPDATE event_definitions
             SET status='active', current_version=$2, draft='null'::jsonb, revision=revision+1, updated_at=clock_timestamp()
             WHERE id=$1 RETURNING revision",
        ).bind(id).bind(number).fetch_one(&mut **txn).await?;
        Ok(ActivatedCustomDefinition {
            id: version,
            version: number,
            new_definition_revision,
        })
    }

    pub async fn retire_custom_definition(
        &self,
        request: ManageCustomDefinition<'_>,
        id: Uuid,
        revision: i32,
    ) -> Result<StoredCustomDefinition, CustomRegistryError> {
        let mut txn = self.begin_definition_management(request).await?;
        lock_definition(&mut txn, request.owner_id, id, revision).await?;
        let row = sqlx::query_as::<_, DefinitionRow>(
            "UPDATE event_definitions SET status='retired', revision=revision+1, updated_at=now()
             WHERE id=$1 RETURNING id,key,status,revision,current_version",
        )
        .bind(id)
        .fetch_one(&mut *txn)
        .await?;
        sqlx::query(
            "UPDATE pending_questions SET status='cancelled'
             WHERE kind='tracker_reminder' AND status IN ('pending','sending','uncertain')
               AND evidence->>'tracker_id' IN (SELECT id::text FROM tracker_configs
                   WHERE definition_id=$1 AND owner_id=$2)",
        )
        .bind(id)
        .bind(request.owner_id)
        .execute(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(stored(row))
    }

    pub(super) async fn begin_definition_management(
        &self,
        request: ManageCustomDefinition<'_>,
    ) -> Result<Transaction<'static, Postgres>, CustomRegistryError> {
        if !request.may_manage_definitions {
            return Err(CustomRegistryError::Forbidden);
        }
        self.begin_diary_write(request.owner_id)
            .await
            .map_err(CustomRegistryError::Diary)
    }
}

type DefinitionRow = (Uuid, String, String, i32, Option<i32>);

fn stored((id, key, status, revision, current_version): DefinitionRow) -> StoredCustomDefinition {
    StoredCustomDefinition {
        id,
        key,
        status,
        revision,
        current_version,
    }
}

fn draft_document(contract: &CustomDefinition, actor: &str) -> Value {
    let mut document = contract.contract().clone();
    document["actor"] = json!(actor);
    document
}

async fn lock_definition(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    id: Uuid,
    revision: i32,
) -> Result<(StoredCustomDefinition, Option<String>), CustomRegistryError> {
    type DefinitionWithDraft = (Uuid, String, String, i32, Option<i32>, Option<String>);
    let (id, key, status, available, current_version, draft) =
        sqlx::query_as::<_, DefinitionWithDraft>(
            "SELECT id,key,status,revision,current_version,NULLIF(draft,'null'::jsonb)::text
             FROM event_definitions WHERE id=$1 AND owner_id=$2 AND namespace='user'
             FOR UPDATE",
        )
        .bind(id)
        .bind(owner)
        .fetch_optional(&mut **txn)
        .await?
        .ok_or(CustomRegistryError::Missing)?;
    if available != revision {
        return Err(CustomRegistryError::RevisionChanged);
    }
    Ok((stored((id, key, status, available, current_version)), draft))
}

async fn check_field_identities(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    contract: &CustomDefinition,
) -> Result<(), CustomRegistryError> {
    let historical = sqlx::query_scalar::<_, String>(
        "SELECT field_metadata::text FROM event_definition_versions WHERE definition_id = $1 ORDER BY version",
    ).bind(id).fetch_all(&mut **txn).await?;
    let mut names = BTreeMap::new();
    let mut identities = BTreeMap::new();
    for version in historical {
        let fields: Value = serde_json::from_str(&version)
            .map_err(|_| CustomRegistryError::InvalidStoredContract)?;
        for (name, field) in fields
            .as_object()
            .ok_or(CustomRegistryError::InvalidStoredContract)?
        {
            let id = field["id"]
                .as_str()
                .ok_or(CustomRegistryError::InvalidStoredContract)?;
            if names
                .insert(name.clone(), id.to_owned())
                .is_some_and(|old| old != id)
                || identities
                    .insert(id.to_owned(), name.clone())
                    .is_some_and(|old| old != *name)
            {
                return Err(CustomRegistryError::InvalidStoredContract);
            }
        }
    }
    let incoming = contract.contract()["fields"]
        .as_object()
        .ok_or(CustomRegistryError::InvalidStoredContract)?;
    for (name, field) in incoming {
        let id = field["id"]
            .as_str()
            .ok_or(CustomRegistryError::InvalidStoredContract)?;
        if names.get(name).is_some_and(|stored| stored != id)
            || identities.get(id).is_some_and(|stored| stored != name)
        {
            return Err(CustomRegistryError::FieldIdentityChanged);
        }
    }
    Ok(())
}
