use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fmt,
    sync::LazyLock,
};

use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{FromRow, Postgres, Row, Transaction, postgres::PgRow};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness};

const WRITER_LOCK: i64 = 72104619;
const POLICY_LOCK: i64 = 72104621;
const CONTEXT_DELIVERY_LOCK: i64 = 72104643;

#[derive(Debug)]
pub enum ScenarioPackError {
    Forbidden,
    UnknownOwner,
    UnknownPack,
    InvalidSelection,
    RevisionChanged,
    IncompleteCatalog,
    Busy,
    Storage(ReadinessError),
    Database,
}

impl fmt::Display for ScenarioPackError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Forbidden => "Scenario pack permission is required",
            Self::UnknownOwner => "Scenario pack owner is unavailable",
            Self::UnknownPack => "Scenario pack not found",
            Self::InvalidSelection => "Scenario pack settings are invalid",
            Self::RevisionChanged => "Scenario pack changed; reload before editing",
            Self::IncompleteCatalog => "Scenario pack settings are incomplete",
            Self::Busy => "Scenario pack settings are busy",
            Self::Storage(_) => "Scenario pack storage is unavailable",
            Self::Database => "Scenario pack settings could not be saved",
        })
    }
}

impl Error for ScenarioPackError {}

impl From<sqlx::Error> for ScenarioPackError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

#[derive(Deserialize)]
struct ScenarioPack {
    key: &'static str,
    labels: BTreeMap<&'static str, &'static str>,
    definitions: Vec<&'static str>,
    forms: Vec<&'static str>,
    rules: Vec<&'static str>,
    analysis: Vec<&'static str>,
}

static CATALOG: LazyLock<Option<Vec<ScenarioPack>>> =
    LazyLock::new(|| serde_json::from_str(include_str!("../contracts/scenario-packs.json")).ok());

pub(crate) fn pack_keys() -> Result<impl Iterator<Item = &'static str>, ScenarioPackError> {
    let catalog = CATALOG
        .as_ref()
        .ok_or(ScenarioPackError::IncompleteCatalog)?;
    Ok(catalog.iter().map(|pack| pack.key))
}

#[derive(Clone)]
pub struct PackSelection {
    pub revision: i64,
    pub tracking_enabled: bool,
    pub collection_enabled: bool,
    pub reminders_enabled: bool,
    pub visible: bool,
    pub llm_enabled: bool,
    pub outcome_goal: Option<String>,
}

struct PackRow {
    pack_key: String,
    revision: i32,
    tracking_enabled: bool,
    collection_enabled: bool,
    reminders_enabled: bool,
    visible: bool,
    llm_enabled: bool,
    outcome_goal: Option<String>,
}

impl FromRow<'_, PgRow> for PackRow {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            pack_key: row.try_get("pack_key")?,
            revision: row.try_get("revision")?,
            tracking_enabled: row.try_get("tracking_enabled")?,
            collection_enabled: row.try_get("collection_enabled")?,
            reminders_enabled: row.try_get("reminders_enabled")?,
            visible: row.try_get("visible")?,
            llm_enabled: row.try_get("llm_enabled")?,
            outcome_goal: row.try_get("outcome_goal")?,
        })
    }
}

impl Database {
    /// Return the trusted packs and initialize owner defaults on first use.
    pub async fn scenario_packs(
        &self,
        owner: Uuid,
        can_read_diary: bool,
    ) -> Result<Vec<Value>, ScenarioPackError> {
        if !can_read_diary {
            return Err(ScenarioPackError::Forbidden);
        }
        let mut txn = self.begin_scenario_write(owner).await?;
        let result = scenario_packs_in(&mut txn, owner).await?;
        txn.commit().await?;
        Ok(result)
    }

    /// Save the five independent preferences under a revision fence.
    pub async fn configure_scenario_pack(
        &self,
        owner: Uuid,
        may_configure: bool,
        key: &str,
        selection: &PackSelection,
    ) -> Result<Value, ScenarioPackError> {
        if !may_configure {
            return Err(ScenarioPackError::Forbidden);
        }
        let catalog = catalog()?;
        let pack = catalog
            .iter()
            .find(|pack| pack.key == key)
            .ok_or(ScenarioPackError::UnknownPack)?;
        if selection.revision < 1
            || selection
                .outcome_goal
                .as_ref()
                .is_some_and(|goal| goal.chars().count() > 500)
        {
            return Err(ScenarioPackError::InvalidSelection);
        }
        let mut txn = self.begin_scenario_write(owner).await?;
        ensure_scenario_packs(&mut txn, owner, catalog, None).await?;
        let result = save_pack(&mut txn, owner, pack, selection).await?;
        txn.commit().await?;
        Ok(result)
    }

    pub(crate) async fn begin_scenario_write(
        &self,
        owner: Uuid,
    ) -> Result<Transaction<'static, Postgres>, ScenarioPackError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(ScenarioPackError::Storage)?;
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(WRITER_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !locked {
            return Err(ScenarioPackError::Busy);
        }
        let exists: bool = sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM people WHERE id=$1)")
            .bind(owner)
            .fetch_one(&mut *txn)
            .await?;
        if !exists {
            return Err(ScenarioPackError::UnknownOwner);
        }
        Ok(txn)
    }
}

pub(crate) async fn scenario_packs_in(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<Vec<Value>, ScenarioPackError> {
    let catalog = catalog()?;
    ensure_scenario_packs(txn, owner, catalog, None).await?;
    let mut rows = pack_rows(txn, owner, catalog).await?;
    catalog
        .iter()
        .map(|pack| {
            let row = rows
                .remove(pack.key)
                .ok_or(ScenarioPackError::IncompleteCatalog)?;
            Ok(state(pack, row))
        })
        .collect()
}

pub(crate) async fn apply_onboarding_packs(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    selected: &BTreeSet<String>,
    reminders: &BTreeSet<String>,
    llm: bool,
) -> Result<Vec<Value>, ScenarioPackError> {
    let catalog = catalog()?;
    let keys: BTreeSet<_> = catalog.iter().map(|pack| pack.key).collect();
    if selected
        .union(reminders)
        .any(|key| !keys.contains(key.as_str()))
    {
        return Err(ScenarioPackError::UnknownPack);
    }
    ensure_scenario_packs(txn, owner, catalog, Some(false)).await?;
    let mut rows = pack_rows(txn, owner, catalog).await?;
    let mut current = Vec::with_capacity(catalog.len());
    for pack in catalog {
        let row = rows
            .remove(pack.key)
            .ok_or(ScenarioPackError::IncompleteCatalog)?;
        let enabled = selected.contains(pack.key);
        current.push(
            save_pack(
                txn,
                owner,
                pack,
                &PackSelection {
                    revision: i64::from(row.revision),
                    tracking_enabled: enabled,
                    collection_enabled: enabled,
                    reminders_enabled: reminders.contains(pack.key),
                    visible: enabled,
                    llm_enabled: enabled && llm,
                    outcome_goal: row.outcome_goal,
                },
            )
            .await?,
        );
    }
    Ok(current)
}

async fn pack_rows(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    catalog: &[ScenarioPack],
) -> Result<BTreeMap<String, PackRow>, sqlx::Error> {
    let keys: Vec<_> = catalog.iter().map(|pack| pack.key).collect();
    let rows = sqlx::query_as::<_, PackRow>(
        "SELECT pack_key,revision,tracking_enabled,collection_enabled,
                reminders_enabled,visible,llm_enabled,outcome_goal
         FROM module_configs WHERE owner_id=$1 AND pack_key=ANY($2::text[])",
    )
    .bind(owner)
    .bind(keys)
    .fetch_all(&mut **txn)
    .await?;
    Ok(rows
        .into_iter()
        .map(|row| (row.pack_key.clone(), row))
        .collect())
}

async fn save_pack(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    pack: &ScenarioPack,
    selection: &PackSelection,
) -> Result<Value, ScenarioPackError> {
    let key = pack.key;
    let old: (i32, bool) = sqlx::query_as(
        "SELECT revision,llm_enabled FROM module_configs
             WHERE owner_id=$1 AND pack_key=$2 FOR UPDATE",
    )
    .bind(owner)
    .bind(key)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(ScenarioPackError::IncompleteCatalog)?;
    if i64::from(old.0) != selection.revision {
        return Err(ScenarioPackError::RevisionChanged);
    }
    if old.1 && !selection.llm_enabled {
        forget_shared_context(txn, owner, key).await?;
    }
    let row = sqlx::query_as::<_, PackRow>(
        "UPDATE module_configs SET tracking_enabled=$3,collection_enabled=$4,
                 reminders_enabled=$5,visible=$6,llm_enabled=$7,outcome_goal=$8,
                 revision=revision+1,updated_at=clock_timestamp()
             WHERE owner_id=$1 AND pack_key=$2
             RETURNING pack_key,revision,tracking_enabled,collection_enabled,
                 reminders_enabled,visible,llm_enabled,outcome_goal",
    )
    .bind(owner)
    .bind(key)
    .bind(selection.tracking_enabled)
    .bind(selection.collection_enabled)
    .bind(selection.reminders_enabled)
    .bind(selection.visible)
    .bind(selection.llm_enabled)
    .bind(&selection.outcome_goal)
    .fetch_one(&mut **txn)
    .await?;
    if !row.reminders_enabled || (!row.tracking_enabled && matches!(key, "caffeine" | "migraine")) {
        let question_kind = match key {
            "general_diary" | "wellbeing" => Some("context"),
            "caffeine" => Some("caffeine"),
            "migraine" => Some("migraine"),
            _ => None,
        };
        if let Some(kind) = question_kind {
            sqlx::query(
                "UPDATE pending_questions SET status='cancelled' WHERE kind=$1
                     AND status IN ('pending','sending','sent','uncertain')",
            )
            .bind(kind)
            .execute(&mut **txn)
            .await?;
        }
    }
    Ok(state(pack, row))
}

fn catalog() -> Result<&'static [ScenarioPack], ScenarioPackError> {
    CATALOG
        .as_deref()
        .ok_or(ScenarioPackError::IncompleteCatalog)
}

async fn ensure_scenario_packs(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    catalog: &[ScenarioPack],
    legacy_install: Option<bool>,
) -> Result<(), ScenarioPackError> {
    let existing: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM module_configs WHERE owner_id=$1)")
            .bind(owner)
            .fetch_one(&mut **txn)
            .await?;
    if existing {
        return Ok(());
    }
    let legacy = if let Some(legacy) = legacy_install {
        legacy
    } else {
        sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM events)
             OR EXISTS (SELECT 1 FROM activities)
             OR EXISTS (SELECT 1 FROM health_days)
             OR EXISTS (SELECT 1 FROM measurements)
             OR EXISTS (SELECT 1 FROM metric_observations)
             OR EXISTS (SELECT 1 FROM source_payloads)
             OR EXISTS (SELECT 1 FROM timeline_intervals)
             OR EXISTS (SELECT 1 FROM app_state WHERE key IN
                 ('preferences:personal-goals','migration:legacy-scenario-packs'))",
        )
        .fetch_one(&mut **txn)
        .await?
    };
    for pack in catalog {
        let enabled = legacy || pack.key == "general_diary";
        sqlx::query(
            "INSERT INTO module_configs (
                id,owner_id,pack_key,tracking_enabled,collection_enabled,
                reminders_enabled,visible,llm_enabled,settings,revision)
             VALUES ($1,$2,$3,$4,$4,$4,$4,$5,'{}'::jsonb,1)
             ON CONFLICT (owner_id,pack_key) DO NOTHING",
        )
        .bind(Uuid::new_v4())
        .bind(owner)
        .bind(pack.key)
        .bind(enabled)
        .bind(legacy)
        .execute(&mut **txn)
        .await?;
    }
    Ok(())
}

pub(crate) async fn model_disallowed_event_kinds(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<Vec<&'static str>, ScenarioPackError> {
    let rows: Vec<(String, bool)> = sqlx::query_as(
        "SELECT pack_key,llm_enabled FROM module_configs
         JOIN people ON people.id=module_configs.owner_id WHERE people.singleton",
    )
    .fetch_all(&mut **txn)
    .await?;
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let allowed: std::collections::BTreeSet<_> = rows
        .iter()
        .filter_map(|(key, enabled)| enabled.then_some(key.as_str()))
        .collect();
    Ok(catalog()?
        .iter()
        .filter(|pack| !allowed.contains(pack.key))
        .flat_map(|pack| pack.definitions.iter().copied())
        .collect())
}

fn state(pack: &ScenarioPack, row: PackRow) -> Value {
    json!({
        "key": pack.key, "labels": pack.labels, "definitions": pack.definitions,
        "forms": pack.forms, "rules": pack.rules, "analysis": pack.analysis,
        "tracking_enabled": row.tracking_enabled, "collection_enabled": row.collection_enabled,
        "reminders_enabled": row.reminders_enabled, "visible": row.visible,
        "llm_enabled": row.llm_enabled, "outcome_goal": row.outcome_goal, "revision": row.revision,
    })
}

async fn forget_shared_context(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    key: &str,
) -> Result<(), ScenarioPackError> {
    sqlx::query("SELECT pg_advisory_xact_lock_shared($1)")
        .bind(POLICY_LOCK)
        .execute(&mut **txn)
        .await?;
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(CONTEXT_DELIVERY_LOCK)
        .execute(&mut **txn)
        .await?;
    sqlx::query(
        "WITH pending AS (
             DELETE FROM app_state WHERE starts_with(key,'analysis:conversation:pending')
             RETURNING value #>> '{turn,update_id}' AS update_id
         ), remembered AS (
             SELECT turn->>'update_id' AS update_id FROM app_state
             CROSS JOIN LATERAL jsonb_array_elements(value->'turns') AS turn
             WHERE key='analysis:conversation'
         ), retired AS (SELECT update_id FROM pending UNION SELECT update_id FROM remembered)
         UPDATE app_state SET value=value || $1::jsonb, updated_at=now()
         WHERE starts_with(key,'telegram:reply:')
           AND (value->>'kind'='analysis'
             OR key IN (SELECT 'telegram:reply:' || update_id FROM retired))",
    )
    .bind(
        json!({
            "text": "Контекст анализа удалён.",
            "status": "forgotten", "keyboard": false, "kind": "analysis",
        })
        .to_string(),
    )
    .execute(&mut **txn)
    .await?;
    sqlx::query(
        "INSERT INTO app_state (key,value) VALUES ('analysis:conversation',$1::jsonb)
         ON CONFLICT (key) DO UPDATE SET value=excluded.value",
    )
    .bind(json!({"epoch": Uuid::new_v4().to_string(), "turns":[]}).to_string())
    .execute(&mut **txn)
    .await?;
    sqlx::query(
        "UPDATE conversations SET memory_epoch=gen_random_uuid(), state='{}'::jsonb, updated_at=now()
         WHERE owner_id=$1",
    ).bind(owner).execute(&mut **txn).await?;
    sqlx::query(
        "DELETE FROM app_state WHERE starts_with(key,'conversation:pending')
         AND (value->>'pack' IS NULL OR value->>'pack'=$1)",
    )
    .bind(key)
    .execute(&mut **txn)
    .await?;
    Ok(())
}

pub(crate) async fn source_instance_selected(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT NOT EXISTS (SELECT 1 FROM app_state WHERE key='preferences:onboarding')
            OR EXISTS (SELECT 1 FROM app_state WHERE key='preferences:onboarding'
                AND jsonb_typeof(value->'source_instance_ids')='array'
                AND (value->'source_instance_ids') ? $1)",
    )
    .bind(instance)
    .fetch_one(&mut **txn)
    .await
}
