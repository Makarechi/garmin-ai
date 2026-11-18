use std::{collections::BTreeMap, error::Error, fmt, sync::LazyLock, time::Duration};

use ga_domain::{community_pack::CommunityPack, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::TrackerSetupStoreError;
use crate::{Database, diary::ManageCustomDefinition, scenario_packs::pack_keys};

const PREVIEW: &str = "community-pack-preview:";
const IMPORTED: &str = "community-pack-import:";
static CATALOG: LazyLock<Option<Vec<Value>>> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../../../contracts/community-packs.json")).ok()
});

#[derive(Debug)]
pub enum CommunityPackError {
    Invalid,
    PreviewChanged,
    Conflict,
    Setup(TrackerSetupStoreError),
    Database,
}

impl fmt::Display for CommunityPackError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Invalid => "Invalid arguments",
            Self::PreviewChanged => "Pack preview changed; preview it again",
            Self::Conflict => "Pack version or tracker key conflicts with an existing installation; rename and preview again",
            Self::Setup(_) | Self::Database => "Community pack could not be saved",
        })
    }
}
impl Error for CommunityPackError {}
impl From<sqlx::Error> for CommunityPackError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}
impl From<TrackerSetupStoreError> for CommunityPackError {
    fn from(error: TrackerSetupStoreError) -> Self {
        Self::Setup(error)
    }
}

pub fn parse_community_pack(value: &Value) -> Result<CommunityPack, CommunityPackError> {
    let known = pack_keys()
        .map_err(|_| CommunityPackError::Database)?
        .collect::<Vec<_>>();
    CommunityPack::from_json(value, &known).map_err(|_| CommunityPackError::Invalid)
}

pub fn community_pack_catalog() -> Result<&'static [Value], CommunityPackError> {
    CATALOG.as_deref().ok_or(CommunityPackError::Database)
}

impl Database {
    pub async fn preview_community_pack(
        &self,
        actor: ManageCustomDefinition<'_>,
        pack: &CommunityPack,
        reveal_dependencies: bool,
    ) -> Result<Value, CommunityPackError> {
        let work = async {
            let mut txn = self
                .begin_definition_management(actor)
                .await
                .map_err(TrackerSetupStoreError::from)?;
            let changes = changes(&mut txn, actor.owner_id, pack).await?;
            let token = token();
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            sqlx::query(
                "DELETE FROM app_state WHERE starts_with(key,$1) AND value->>'expires_at'<$2",
            )
            .bind(PREVIEW)
            .bind(timestamp(at)?)
            .execute(&mut *txn)
            .await?;
            let preview =
                json!({"hash":pack.hash(),"expires_at":timestamp(at+time::Duration::minutes(15))?});
            save(&mut txn, &format!("{PREVIEW}{token}"), &preview).await?;
            let mut required = Vec::new();
            for key in pack.document()["required_packs"]
                .as_array()
                .ok_or(CommunityPackError::Invalid)?
            {
                let key = key.as_str().ok_or(CommunityPackError::Invalid)?;
                let enabled: Option<(bool, bool)> = if reveal_dependencies {
                    let any: bool = sqlx::query_scalar(
                        "SELECT EXISTS (SELECT FROM module_configs WHERE owner_id=$1)",
                    )
                    .bind(actor.owner_id)
                    .fetch_one(&mut *txn)
                    .await?;
                    if any {
                        Some(sqlx::query_as("SELECT tracking_enabled,collection_enabled FROM module_configs WHERE owner_id=$1 AND pack_key=$2")
                            .bind(actor.owner_id).bind(key).fetch_optional(&mut *txn).await?.unwrap_or((false,false)))
                    } else {
                        Some((true, true))
                    }
                } else {
                    None
                };
                required.push(json!({"key":key,"tracking_enabled":enabled.map(|row|row.0),"collection_enabled":enabled.map(|row|row.1)}));
            }
            txn.commit().await?;
            Ok(json!({
                "confirmation_token":token,"pack_key":pack.key(),"version":pack.version(),
                "permissions":{"create_trackers":changes.iter().any(|row|row["status"]=="create"),
                    "enable_reminders":false,"enable_external_sources":false,"grant_model_or_channel_access":false},
                "changes":changes,"required_packs":required,"analysis":pack.document()["analysis"],
                "limitations":pack.document()["limitations"],
            }))
        };
        tokio::time::timeout(Duration::from_millis(2500), work)
            .await
            .map_err(|_| CommunityPackError::Database)?
    }

    pub async fn import_community_pack(
        &self,
        actor: ManageCustomDefinition<'_>,
        pack: &CommunityPack,
        confirmation: &str,
    ) -> Result<Value, CommunityPackError> {
        let work = async {
            let mut txn = self
                .begin_definition_management(actor)
                .await
                .map_err(TrackerSetupStoreError::from)?;
            let key = format!("{PREVIEW}{confirmation}");
            let preview: Option<String> =
                sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1 FOR UPDATE")
                    .bind(&key)
                    .fetch_optional(&mut *txn)
                    .await?;
            let preview = preview
                .and_then(|wire| serde_json::from_str::<Value>(&wire).ok())
                .ok_or(CommunityPackError::PreviewChanged)?;
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            if preview["hash"] != pack.hash()
                || !StoredInstant::from_legacy_input(&preview["expires_at"])
                    .is_some_and(|expires| OffsetDateTime::from(expires) > at)
            {
                return Err(CommunityPackError::PreviewChanged);
            }
            let changes = changes(&mut txn, actor.owner_id, pack).await?;
            if changes.iter().any(|row| {
                matches!(
                    row["status"].as_str(),
                    Some("conflict" | "version_conflict")
                )
            }) {
                return Err(CommunityPackError::Conflict);
            }
            sqlx::query("DELETE FROM app_state WHERE key=$1")
                .bind(key)
                .execute(&mut *txn)
                .await?;
            let mut created = Vec::new();
            for (draft, change) in pack.trackers().iter().zip(&changes) {
                if change["status"] == "create" {
                    created.push(Self::create_tracker_in(&mut txn, actor, draft).await?);
                }
            }
            let mut definitions = serde_json::Map::new();
            for draft in pack.trackers() {
                let key = draft.definition().key();
                let (id, version): (Uuid, Option<i32>) = sqlx::query_as(
                    "SELECT id,current_version FROM event_definitions WHERE owner_id=$1 AND key=$2",
                )
                .bind(actor.owner_id)
                .bind(key)
                .fetch_one(&mut *txn)
                .await?;
                definitions.insert(
                    key.to_owned(),
                    json!({"id":id.to_string(),"version":version,"draft_hash":draft.hash()}),
                );
            }
            let state_key = format!("{IMPORTED}{}:{}", pack.key(), pack.version());
            let existing: bool =
                sqlx::query_scalar("SELECT EXISTS (SELECT FROM app_state WHERE key=$1)")
                    .bind(&state_key)
                    .fetch_one(&mut *txn)
                    .await?;
            if !existing || !created.is_empty() {
                save(&mut txn,&state_key,&json!({"hash":pack.hash(),"definitions":definitions,"installed_at":timestamp(at)?})).await?;
            }
            txn.commit().await?;
            Ok(
                json!({"pack_key":pack.key(),"version":pack.version(),"created":created,"changes":changes}),
            )
        };
        tokio::time::timeout(Duration::from_secs(5), work)
            .await
            .map_err(|_| CommunityPackError::Database)?
    }
}

async fn changes(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    pack: &CommunityPack,
) -> Result<Vec<Value>, CommunityPackError> {
    let state_key = format!("{IMPORTED}{}:{}", pack.key(), pack.version());
    let saved: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(&state_key)
            .fetch_optional(&mut **txn)
            .await?;
    let saved = saved
        .map(|wire| serde_json::from_str::<Value>(&wire))
        .transpose()
        .map_err(|_| CommunityPackError::Database)?;
    if saved
        .as_ref()
        .is_some_and(|saved| saved["hash"] != pack.hash())
    {
        return Ok(pack.trackers().iter().map(|draft|json!({"definition_key":draft.definition().key(),"status":"version_conflict"})).collect());
    }
    let prior: Vec<(String, String)> =
        sqlx::query_as("SELECT key,value::text FROM app_state WHERE starts_with(key,$1)")
            .bind(format!("{IMPORTED}{}:", pack.key()))
            .fetch_all(&mut **txn)
            .await?;
    let mut prior_definitions = BTreeMap::<String, Vec<Value>>::new();
    for (key, wire) in prior {
        if key
            .rsplit(':')
            .next()
            .and_then(|version| version.parse::<i64>().ok())
            .is_some_and(|version| version < pack.version())
        {
            let state: Value =
                serde_json::from_str(&wire).map_err(|_| CommunityPackError::Database)?;
            if let Some(definitions) = state["definitions"].as_object() {
                for (key, value) in definitions {
                    prior_definitions
                        .entry(key.clone())
                        .or_default()
                        .push(value.clone());
                }
            }
        }
    }
    let mut changes = Vec::new();
    for draft in pack.trackers() {
        let key = draft.definition().key();
        let row: Option<(Uuid, String, Option<i32>)> = sqlx::query_as(
            "SELECT id,status,current_version FROM event_definitions WHERE owner_id=$1 AND key=$2",
        )
        .bind(owner)
        .bind(key)
        .fetch_optional(&mut **txn)
        .await?;
        let status = if let Some((id, status, version)) = row {
            let id = id.to_string();
            let installed = saved
                .as_ref()
                .and_then(|saved| saved["definitions"].get(key));
            if status != "active" {
                "conflict"
            } else if installed.is_some_and(|entry| {
                entry["id"] == id && entry["version"].as_i64() == version.map(i64::from)
            }) || prior_definitions.get(key).is_some_and(|entries| {
                entries.iter().any(|entry| {
                    entry["id"] == id
                        && entry["version"].as_i64() == version.map(i64::from)
                        && entry["draft_hash"] == draft.hash()
                })
            }) {
                "already_installed"
            } else {
                "conflict"
            }
        } else {
            "create"
        };
        changes.push(json!({"definition_key":key,"status":status}));
    }
    Ok(changes)
}

async fn save(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    value: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
    )
    .bind(key)
    .bind(value.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}
fn timestamp(at: OffsetDateTime) -> Result<String, CommunityPackError> {
    StoredInstant::try_from(at)
        .map(|at| at.to_string().replace('Z', "+00:00"))
        .map_err(|_| CommunityPackError::Database)
}
fn token() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}
