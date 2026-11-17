use std::{collections::BTreeMap, error::Error, fmt, sync::LazyLock};

use ga_domain::{event_contract::metric_definition_hash, metric_contract::UserMetricContract};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness};

const METRIC_REGISTRY_LOCK: i64 = 72104629;
const DIARY_WRITER_LOCK: i64 = 72104619;

#[derive(Debug)]
pub enum MetricRegistryError {
    Unavailable(ReadinessError),
    InvalidCatalog,
    InvalidRecordedVersion,
    Forbidden,
    OwnerMissing,
    Busy,
    Database,
}

impl fmt::Display for MetricRegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Unavailable(_) => "Metric storage is unavailable",
            Self::InvalidCatalog => "The built-in metric registry is invalid",
            Self::InvalidRecordedVersion => "The stored metric definition is invalid",
            Self::Forbidden => "Metric definition management permission is required",
            Self::OwnerMissing => "Metric owner could not be verified",
            Self::Busy => "Metric registry writes are busy",
            Self::Database => "The metric registry could not be saved",
        })
    }
}

impl Error for MetricRegistryError {}

impl From<sqlx::Error> for MetricRegistryError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MetricVersion {
    pub id: Uuid,
    pub version: i32,
}

pub type SystemMetricVersion = MetricVersion;

#[derive(Deserialize)]
struct CatalogEntry {
    hash: String,
    contract: Value,
}

type Catalog = BTreeMap<String, CatalogEntry>;

static METRICS: LazyLock<Option<Catalog>> = LazyLock::new(|| {
    let catalog: Catalog =
        serde_json::from_str(include_str!("../contracts/system-metrics.json")).ok()?;
    for (key, entry) in &catalog {
        if entry.contract["key"] != format!("system.{key}")
            || metric_definition_hash(&entry.contract).ok()? != entry.hash
        {
            return None;
        }
    }
    Some(catalog)
});

impl Database {
    /// Store a verified user metric, keeping old versions available to their observations.
    pub async fn register_user_metric(
        &self,
        owner: Uuid,
        authorized: bool,
        contract: &UserMetricContract,
    ) -> Result<MetricVersion, MetricRegistryError> {
        if !authorized {
            return Err(MetricRegistryError::Forbidden);
        }
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(MetricRegistryError::Unavailable)?;
        let held: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(DIARY_WRITER_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !held {
            return Err(MetricRegistryError::Busy);
        }
        let exists: bool = sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM people WHERE id = $1)")
            .bind(owner)
            .fetch_one(&mut *txn)
            .await?;
        if !exists {
            return Err(MetricRegistryError::OwnerMissing);
        }
        let version = register_user_metric_in(&mut txn, owner, contract).await?;
        txn.commit().await?;
        Ok(version)
    }

    /// Ensure the immutable built-in contracts without altering prior observations.
    pub async fn ensure_system_metrics(
        &self,
    ) -> Result<BTreeMap<&'static str, SystemMetricVersion>, MetricRegistryError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(MetricRegistryError::Unavailable)?;
        let versions = ensure_system_metrics_in(&mut txn).await?;
        txn.commit().await?;
        Ok(versions)
    }
}

pub(crate) async fn ensure_system_metrics_in(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<BTreeMap<&'static str, SystemMetricVersion>, MetricRegistryError> {
    let catalog = METRICS
        .as_ref()
        .ok_or(MetricRegistryError::InvalidCatalog)?;
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(METRIC_REGISTRY_LOCK)
        .execute(&mut **txn)
        .await?;
    let mut versions = BTreeMap::new();
    for (key, entry) in catalog {
        let version = ensure_metric(
            txn,
            "system",
            None,
            &format!("system.{key}"),
            &entry.contract,
            &entry.hash,
        )
        .await?;
        versions.insert(key.as_str(), version);
    }
    Ok(versions)
}

pub(crate) async fn register_user_metric_in(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    contract: &UserMetricContract,
) -> Result<MetricVersion, MetricRegistryError> {
    ensure_metric(
        txn,
        "user",
        Some(owner),
        contract.key(),
        contract.contract(),
        contract.hash(),
    )
    .await
}

async fn ensure_metric(
    txn: &mut Transaction<'_, Postgres>,
    namespace: &str,
    owner: Option<Uuid>,
    key: &str,
    contract: &Value,
    hash: &str,
) -> Result<MetricVersion, MetricRegistryError> {
    type Definition = (
        Uuid,
        String,
        Option<Uuid>,
        i32,
        Option<Uuid>,
        Option<String>,
    );
    let existing = sqlx::query_as::<_, Definition>(
        "SELECT metric.id, metric.namespace, metric.owner_id, metric.current_version, current.id, current.schema_hash
         FROM metric_definitions AS metric
         LEFT JOIN metric_definition_versions AS current
           ON current.definition_id = metric.id AND current.version = metric.current_version
         WHERE metric.key = $1 FOR UPDATE OF metric",
    )
    .bind(key)
    .fetch_optional(&mut **txn)
    .await?;
    let (definition, version) = match existing {
        Some((_, recorded_namespace, recorded_owner, version, Some(id), Some(recorded_hash)))
            if recorded_namespace == namespace
                && recorded_owner == owner
                && recorded_hash == hash =>
        {
            return Ok(MetricVersion { id, version });
        }
        Some((definition, recorded_namespace, recorded_owner, version, Some(_), Some(_)))
            if recorded_namespace == namespace && recorded_owner == owner =>
        {
            let version = version
                .checked_add(1)
                .ok_or(MetricRegistryError::InvalidRecordedVersion)?;
            sqlx::query("UPDATE metric_definitions SET current_version = $2 WHERE id = $1")
                .bind(definition)
                .bind(version)
                .execute(&mut **txn)
                .await?;
            (definition, version)
        }
        Some(_) => return Err(MetricRegistryError::InvalidRecordedVersion),
        None => {
            let definition = Uuid::new_v4();
            sqlx::query(
                "INSERT INTO metric_definitions(id, namespace, owner_id, key, status, current_version)
                 VALUES ($1, $2, $3, $4, 'active', 1)",
            )
            .bind(definition)
            .bind(namespace)
            .bind(owner)
            .bind(key)
            .execute(&mut **txn)
            .await?;
            (definition, 1)
        }
    };
    let id = Uuid::new_v4();
    sqlx::query(
        "WITH raw AS (SELECT $5::jsonb AS contract)
         INSERT INTO metric_definition_versions (
             id, definition_id, version, schema_hash, value_kind, unit, dimension, scale_id,
             scale_version, aggregation, coverage_policy, time_semantics, minimum, maximum,
             labels, allowed_methods, category_domain)
         SELECT $1, $2, $3, $4, contract->>'value_kind', contract->>'unit', contract->>'dimension',
             contract->>'scale_id', (contract->>'scale_version')::int, contract->>'aggregation',
             contract->'coverage', contract->>'time_semantics', (contract->>'minimum')::float8,
             (contract->>'maximum')::float8, contract->'labels', contract->'allowed_methods',
             contract->'category_domain'
         FROM raw",
    )
    .bind(id)
    .bind(definition)
    .bind(version)
    .bind(hash)
    .bind(contract.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(MetricVersion { id, version })
}
