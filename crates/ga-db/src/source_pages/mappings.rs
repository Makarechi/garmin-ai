use std::{error::Error, fmt, time::Duration};

use ga_domain::{
    legacy_json::{Utf8Layout, encode},
    sha256_hex,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    SourcePageError, WRITER_LOCK, check_source_storage, projection::MappingDraft, read_state,
    valid_sha256, write_state,
};

const PREVIEW: &str = "source-mapping-preview:";
const MAPPING: &str = "source-metric-mapping:";

#[derive(Debug)]
pub enum SourceMappingError {
    Invalid,
    PreviewChanged,
    ContractChanged,
    MappingConflict,
    IdentityConfirmation,
    Locked,
    Missing,
    Normalized,
    Source(SourcePageError),
}

impl fmt::Display for SourceMappingError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Invalid => "Invalid arguments",
            Self::PreviewChanged => "Source mapping preview changed; preview it again",
            Self::ContractChanged => "Source or metric changed; preview the mapping again",
            Self::MappingConflict => "Source and metric already have a different confirmed mapping",
            Self::IdentityConfirmation => "Source identity needs a current explicit confirmation",
            Self::Locked => "Tracker or metric is changing; retry the confirmation",
            Self::Missing => "Record not found",
            Self::Normalized => "Mapping with normalized data cannot be removed here",
            Self::Source(_) => "Source mapping storage is unavailable",
        })
    }
}
impl Error for SourceMappingError {}
impl From<SourcePageError> for SourceMappingError {
    fn from(error: SourcePageError) -> Self {
        Self::Source(error)
    }
}
impl From<sqlx::Error> for SourceMappingError {
    fn from(error: sqlx::Error) -> Self {
        if error
            .as_database_error()
            .is_some_and(|error| error.code().as_deref() == Some("55P03"))
        {
            Self::Locked
        } else {
            Self::Source(SourcePageError::Database)
        }
    }
}

impl crate::Database {
    pub async fn preview_source_metric_mapping(
        &self,
        owner: Uuid,
        draft: &MappingDraft,
        source_hash: &str,
    ) -> Result<Value, SourceMappingError> {
        if !valid_sha256(source_hash) {
            return Err(SourceMappingError::Invalid);
        }
        let work = async {
            let mut txn = self.begin_source_mapping(owner, true).await?;
            let metric = resolve(&mut txn, owner, draft, false).await?;
            let data = draft.document()?;
            let identity = digest(&json!([
                owner.to_string(),
                draft.instance(),
                draft.version()?.to_string()
            ]))?;
            let existing = read_state(&mut txn, &format!("{MAPPING}{identity}")).await?;
            let reconfirmation = existing.as_ref().filter(|saved| {
                saved["state"] == "normalized"
                    && saved["mapping"] == data
                    && saved["owner_id"] == owner.to_string()
                    && saved["metric"] == metric
                    && saved["source_hash"] != source_hash
            }).map(|_| json!({
                "preserves_record_identity": true,
                "warning": "Existing record IDs will refer to the same history; confirm only if the configured source retains those identities.",
            }));
            let token = token();
            let now = now(&mut txn).await?;
            sqlx::query(
                "DELETE FROM app_state WHERE starts_with(key,$1) AND value->>'expires_at'<$2",
            )
            .bind(PREVIEW)
            .bind(timestamp(now)?)
            .execute(&mut *txn)
            .await?;
            let preview = json!({
                "draft_hash": digest(&data)?,
                "metric_hash": digest(&metric)?,
                "source_hash": source_hash,
                "owner_id": owner.to_string(),
                "existing_mapping_hash": existing.as_ref().map(digest).transpose()?,
                "expires_at": timestamp(now + time::Duration::minutes(15))?,
            });
            write_state(&mut txn, &format!("{PREVIEW}{token}"), &preview).await?;
            txn.commit().await?;
            Ok(
                json!({"confirmation_token":token,"mapping":data,"metric":metric,"reconfirmation":reconfirmation}),
            )
        };
        tokio::time::timeout(Duration::from_millis(1800), work)
            .await
            .map_err(|_| SourcePageError::Database)?
    }

    pub async fn confirm_source_metric_mapping(
        &self,
        owner: Uuid,
        draft: &MappingDraft,
        source_hash: &str,
        confirmation: &str,
        reconfirm_identity: bool,
    ) -> Result<Value, SourceMappingError> {
        if !valid_sha256(confirmation) || !valid_sha256(source_hash) {
            return Err(SourceMappingError::Invalid);
        }
        let work = async {
            let mut txn = self.begin_source_mapping(owner, true).await?;
            let preview_key = format!("{PREVIEW}{confirmation}");
            let raw: Option<String> =
                sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1 FOR UPDATE")
                    .bind(&preview_key)
                    .fetch_optional(&mut *txn)
                    .await?;
            let preview = raw
                .and_then(|wire| serde_json::from_str::<Value>(&wire).ok())
                .ok_or(SourceMappingError::PreviewChanged)?;
            let data = draft.document()?;
            let clock = now(&mut txn).await?;
            if preview["draft_hash"] != digest(&data)?
                || StoredInstant::from_legacy_input(&preview["expires_at"])
                    .is_none_or(|expiry| OffsetDateTime::from(expiry) <= clock)
            {
                return Err(SourceMappingError::PreviewChanged);
            }
            let metric = resolve(&mut txn, owner, draft, true).await?;
            if preview["metric_hash"] != digest(&metric)?
                || preview["source_hash"] != source_hash
                || preview["owner_id"] != owner.to_string()
            {
                return Err(SourceMappingError::ContractChanged);
            }
            let identity = digest(&json!([
                owner.to_string(),
                draft.instance(),
                draft.version()?.to_string()
            ]))?;
            let key = format!("{MAPPING}{identity}");
            let existing = read_state(&mut txn, &key).await?;
            if existing.as_ref().is_some_and(|saved| {
                saved["mapping"] != data
                    || saved["owner_id"] != owner.to_string()
                    || saved["metric"] != metric
            }) {
                return Err(SourceMappingError::MappingConflict);
            }
            let mut saved = existing.unwrap_or_else(|| {
                json!({
                    "id": identity,
                    "state": "confirmed_not_normalized",
                    "mapping": data,
                    "metric": metric,
                    "source_hash": source_hash,
                    "owner_id": owner.to_string(),
                })
            });
            if saved["source_hash"] != source_hash {
                if saved["state"] != "normalized"
                    || !reconfirm_identity
                    || preview["existing_mapping_hash"] != digest(&saved)?
                {
                    return Err(SourceMappingError::IdentityConfirmation);
                }
                let confirmed = timestamp(clock)?;
                let receipt = json!({
                    "mapping_id": identity,
                    "owner_id": owner.to_string(),
                    "previous_source_hash": saved["source_hash"],
                    "previous_confirmed_at": saved["confirmed_at"],
                    "source_hash": source_hash,
                    "confirmed_at": confirmed,
                });
                write_state(
                    &mut txn,
                    &format!("source-mapping-confirmation:{}", token()),
                    &receipt,
                )
                .await?;
                saved["source_hash"] = json!(source_hash);
                saved["confirmed_at"] = json!(confirmed);
            }
            if saved["confirmed_at"].is_null() {
                saved["confirmed_at"] = json!(timestamp(clock)?);
            }
            write_state(&mut txn, &key, &saved).await?;
            sqlx::query("DELETE FROM app_state WHERE key=$1")
                .bind(preview_key)
                .execute(&mut *txn)
                .await?;
            txn.commit().await?;
            public(&saved)
        };
        tokio::time::timeout(Duration::from_millis(2000), work)
            .await
            .map_err(|_| SourcePageError::Database)?
    }

    pub async fn list_source_metric_mappings(
        &self,
        owner: Uuid,
        limit: u16,
        after: Option<&str>,
    ) -> Result<Value, SourceMappingError> {
        if !(1..=100).contains(&limit) || after.is_some_and(|id| !valid_sha256(id)) {
            return Err(SourceMappingError::Invalid);
        }
        let mut txn = self.begin_source_mapping(owner, false).await?;
        let rows: Vec<String> = sqlx::query_scalar(
            "SELECT value::text FROM app_state WHERE starts_with(key,$1)
            AND value->>'owner_id'=$2 AND ($3::text IS NULL OR key>$1 || $3) ORDER BY key LIMIT $4",
        )
        .bind(MAPPING)
        .bind(owner.to_string())
        .bind(after)
        .bind(i64::from(limit) + 1)
        .fetch_all(&mut *txn)
        .await?;
        let truncated = rows.len() > usize::from(limit);
        let mappings = rows
            .into_iter()
            .take(usize::from(limit))
            .map(|row| {
                let value: Value = serde_json::from_str(&row)
                    .map_err(|_| SourceMappingError::Source(SourcePageError::Database))?;
                public(&value)
            })
            .collect::<Result<Vec<_>, _>>()?;
        let cursor = truncated
            .then(|| mappings.last().map(|row| row["id"].clone()))
            .flatten();
        txn.commit().await?;
        Ok(json!({"mappings":mappings,"next_cursor":cursor}))
    }

    pub async fn delete_source_metric_mapping(
        &self,
        owner: Uuid,
        id: &str,
    ) -> Result<Value, SourceMappingError> {
        if !valid_sha256(id) {
            return Err(SourceMappingError::Invalid);
        }
        let mut txn = self.begin_source_mapping(owner, true).await?;
        let key = format!("{MAPPING}{id}");
        let saved: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1 FOR UPDATE")
                .bind(&key)
                .fetch_optional(&mut *txn)
                .await?;
        let saved = saved
            .and_then(|wire| serde_json::from_str::<Value>(&wire).ok())
            .filter(|saved| saved["owner_id"] == owner.to_string())
            .ok_or(SourceMappingError::Missing)?;
        if saved["state"] != "confirmed_not_normalized" {
            return Err(SourceMappingError::Normalized);
        }
        sqlx::query("DELETE FROM app_state WHERE key=$1")
            .bind(key)
            .execute(&mut *txn)
            .await?;
        txn.commit().await?;
        Ok(json!({"deleted":true}))
    }

    async fn begin_source_mapping(
        &self,
        owner: Uuid,
        write: bool,
    ) -> Result<Transaction<'static, Postgres>, SourceMappingError> {
        let mut txn = if write {
            self.work.begin().await?
        } else {
            self.work
                .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                .await?
        };
        sqlx::query("SET LOCAL statement_timeout='750ms'")
            .execute(&mut *txn)
            .await?;
        check_source_storage(&mut txn, owner).await?;
        if write {
            let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                .bind(WRITER_LOCK)
                .fetch_one(&mut *txn)
                .await?;
            if !locked {
                return Err(SourcePageError::Busy.into());
            }
        }
        Ok(txn)
    }
}

async fn resolve(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    draft: &MappingDraft,
    lock: bool,
) -> Result<Value, SourceMappingError> {
    if !crate::scenario_packs::source_instance_selected(txn, draft.instance()).await? {
        return Err(SourceMappingError::Invalid);
    }
    let mut linked_query=sqlx::QueryBuilder::<Postgres>::new("SELECT definition.id FROM event_metric_mappings binding
        JOIN event_definition_versions version ON version.id=binding.event_definition_version_id
        JOIN event_definitions definition ON definition.id=version.definition_id
        WHERE binding.metric_definition_version_id=$1 AND definition.owner_id=$2
            AND definition.status='active' AND version.version=definition.current_version ORDER BY definition.id LIMIT 1");
    if lock {
        linked_query.push(" FOR UPDATE OF definition NOWAIT");
    }
    let linked: Option<Uuid> = linked_query
        .build_query_scalar()
        .bind(draft.version()?)
        .bind(owner)
        .fetch_optional(&mut **txn)
        .await?;
    if linked.is_none() {
        return Err(SourceMappingError::Invalid);
    }
    let mut query=sqlx::QueryBuilder::<Postgres>::new("SELECT jsonb_build_object('id',definition.key,'version_id',version.id::text,'schema_hash',version.schema_hash,
        'labels',version.labels,'value_kind',version.value_kind,'dimension',version.dimension,'unit',version.unit,
        'minimum',version.minimum,'maximum',version.maximum,'time_semantics',version.time_semantics)::text
        FROM metric_definitions definition JOIN metric_definition_versions version ON version.definition_id=definition.id
        WHERE version.id=$1 AND definition.owner_id=$2 AND definition.namespace='user' AND definition.status='active'
            AND definition.current_version=version.version AND version.value_kind='physical_number'
            AND version.time_semantics='point' AND version.unit=$3");
    if lock {
        query.push(" FOR UPDATE OF definition NOWAIT");
    }
    let raw: Option<String> = query
        .build_query_scalar()
        .bind(draft.version()?)
        .bind(owner)
        .bind(draft.unit())
        .fetch_optional(&mut **txn)
        .await?;
    let mut metric: Value = raw
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .ok_or(SourceMappingError::Invalid)?;
    for key in ["minimum", "maximum"] {
        if let Some(value) = metric[key].as_f64() {
            metric[key] = json!(value);
        }
    }
    Ok(metric)
}

fn digest(value: &Value) -> Result<String, SourceMappingError> {
    let raw = serde_json::to_vec(value).map_err(|_| SourceMappingError::Invalid)?;
    let normalized =
        encode(&raw, Utf8Layout::Compact, 1024 * 1024).map_err(|_| SourceMappingError::Invalid)?;
    Ok(sha256_hex(&normalized))
}
fn token() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}
async fn now(txn: &mut Transaction<'_, Postgres>) -> Result<OffsetDateTime, SourceMappingError> {
    Ok(sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?)
}
fn timestamp(at: OffsetDateTime) -> Result<String, SourceMappingError> {
    StoredInstant::try_from(at)
        .map(|at| at.to_string().replace('Z', "+00:00"))
        .map_err(|_| SourceMappingError::Invalid)
}
fn public(value: &Value) -> Result<Value, SourceMappingError> {
    let keys = ["id", "state", "mapping", "metric", "confirmed_at"];
    let object = value
        .as_object()
        .ok_or(SourceMappingError::Source(SourcePageError::Database))?;
    let mut projected = serde_json::Map::new();
    for key in keys {
        projected.insert(
            key.to_owned(),
            object
                .get(key)
                .cloned()
                .ok_or(SourceMappingError::Source(SourcePageError::Database))?,
        );
    }
    Ok(Value::Object(projected))
}
