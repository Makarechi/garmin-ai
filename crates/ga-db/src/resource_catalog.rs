mod evidence;

pub use evidence::{CatalogEvidence, ModelResourcePage, RESOURCE_CATALOG};

use std::{collections::BTreeSet, error::Error, fmt, io::Write, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::model_consent::ModelCategories;
use serde_json::{Map, Value};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness,
    health_analysis::{HealthAnalysisError, model_metric_packs},
    model_gate::onboarding::onboarding_allows_model,
    replay::{GARMIN_PARSER_VERSION, REPLAY_MUTATION_LOCK, projection_pending},
    scenario_packs::model_disallowed_event_kinds,
};

#[derive(Clone, Copy)]
pub enum ResourceKind {
    Events,
    SystemMetrics,
    TrackerMetrics,
    RawSources,
}

impl ResourceKind {
    pub fn name(self) -> &'static str {
        match self {
            Self::Events => "events",
            Self::SystemMetrics => "system_metrics",
            Self::TrackerMetrics => "tracker_metrics",
            Self::RawSources => "raw_sources",
        }
    }

    fn health(self) -> bool {
        matches!(self, Self::SystemMetrics | Self::RawSources)
    }
}

pub struct ResourcePage<'a> {
    pub kind: ResourceKind,
    pub after_key: Option<&'a str>,
    pub resource_key: Option<&'a str>,
    pub limit: u16,
    pub detail: bool,
}

impl<'a> ResourcePage<'a> {
    pub fn parse(arguments: &'a Map<String, Value>) -> Result<Self, CatalogError> {
        if arguments.keys().any(|key| {
            !matches!(
                key.as_str(),
                "kind" | "after_key" | "resource_key" | "limit" | "detail"
            )
        }) {
            return Err(CatalogError::Arguments);
        }
        let kind = match arguments.get("kind").and_then(Value::as_str) {
            Some("events") => ResourceKind::Events,
            Some("system_metrics") => ResourceKind::SystemMetrics,
            Some("tracker_metrics") => ResourceKind::TrackerMetrics,
            Some("raw_sources") => ResourceKind::RawSources,
            _ => return Err(CatalogError::Arguments),
        };
        let text = |key| match arguments.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(value)) if value.chars().count() <= 200 => Ok(Some(value.as_str())),
            _ => Err(CatalogError::Arguments),
        };
        let limit = match arguments.get("limit") {
            None => 10,
            Some(value) => ga_domain::legacy_json::integer_argument(value)
                .ok_or(CatalogError::Arguments)?
                .try_into()
                .unwrap_or(0),
        };
        let detail = match arguments.get("detail") {
            None => false,
            Some(value) => {
                ga_domain::legacy_json::boolean_argument(value).ok_or(CatalogError::Arguments)?
            }
        };
        Ok(Self {
            kind,
            after_key: text("after_key")?,
            resource_key: text("resource_key")?,
            limit,
            detail,
        })
    }
}

#[derive(Debug)]
pub enum CatalogError {
    Arguments,
    ModelConsent,
    ResultTooLarge,
    Health(HealthAnalysisError),
    Database,
}

impl fmt::Display for CatalogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Arguments => "Resource catalog arguments are invalid",
            Self::ModelConsent => "The resource model category is disabled",
            Self::ResultTooLarge => "The resource page exceeds the model result limit",
            Self::Health(_) => "Resource catalog health data is unavailable",
            Self::Database => "Resource catalog could not be read",
        })
    }
}

impl Error for CatalogError {}

impl From<sqlx::Error> for CatalogError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Discover available resources without revealing unshared tracker facts.
    pub async fn resource_catalog(
        &self,
        page: ResourcePage<'_>,
        model_owner: Option<Uuid>,
        health_scope: bool,
    ) -> Result<JsonDocument, CatalogError> {
        let work = async {
            let mut txn = self
                .work
                .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                .await?;
            sqlx::query("SET LOCAL statement_timeout = '120s'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(|error| CatalogError::Health(HealthAnalysisError::Storage(error)))?;
            let model = model_owner.map(|owner| CatalogModel {
                owner,
                destination: "model:mcp:local",
                channel: None,
            });
            let (output, _) = resource_catalog_in(&mut txn, page, model, health_scope).await?;
            txn.commit().await?;
            Ok(output)
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| CatalogError::Database)?
    }
}

#[derive(Clone, Copy)]
struct CatalogModel<'a> {
    owner: Uuid,
    destination: &'a str,
    channel: Option<&'a str>,
}

async fn resource_catalog_in(
    txn: &mut Transaction<'_, Postgres>,
    page: ResourcePage<'_>,
    model: Option<CatalogModel<'_>>,
    health_scope: bool,
) -> Result<(JsonDocument, BTreeSet<Uuid>), CatalogError> {
    let channel_catalog = model.is_some_and(|model| model.channel.is_some());
    if channel_catalog || !matches!(page.kind, ResourceKind::Events) {
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
            .bind(REPLAY_MUTATION_LOCK)
            .fetch_one(&mut **txn)
            .await?;
        if !locked {
            return Err(CatalogError::Health(HealthAnalysisError::Storage(
                ReadinessError::Unavailable,
            )));
        }
        if (page.kind.health() || channel_catalog)
            && projection_pending(txn, GARMIN_PARSER_VERSION).await?
        {
            return Err(CatalogError::Health(HealthAnalysisError::ReplayPending));
        }
    }
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&mut **txn)
        .await?;
    let mut health_allowed = health_scope;
    let mut denied_events = Vec::new();
    let mut denied_metrics = Vec::new();
    if let Some(model) = model {
        let category = if page.kind.health() {
            ModelCategories::HEALTH
        } else {
            ModelCategories::DIARY
        };
        if model.owner != owner || !onboarding_allows_model(txn, category).await? {
            return Err(CatalogError::ModelConsent);
        }
        health_allowed &= onboarding_allows_model(txn, ModelCategories::HEALTH).await?;
        denied_events = model_disallowed_event_kinds(txn)
            .await
            .map_err(|_| CatalogError::Database)?;
        let packs: Vec<(String, bool)> =
            sqlx::query_as("SELECT pack_key,llm_enabled FROM module_configs WHERE owner_id=$1")
                .bind(owner)
                .fetch_all(&mut **txn)
                .await?;
        if !packs.is_empty() && matches!(page.kind, ResourceKind::SystemMetrics) {
            let enabled: BTreeSet<_> = packs
                .iter()
                .filter_map(|(key, on)| on.then_some(key.as_str()))
                .collect();
            let keys: Vec<String> =
                sqlx::query_scalar("SELECT key FROM metric_definitions WHERE namespace='system'")
                    .fetch_all(&mut **txn)
                    .await?;
            denied_metrics = keys
                .into_iter()
                .filter(|key| {
                    model_metric_packs(key)
                        .iter()
                        .any(|pack| !enabled.contains(pack))
                })
                .collect();
        }
    }
    if !(1..=50).contains(&page.limit)
        || (page.after_key.is_some() && page.resource_key.is_some())
        || (page.detail && page.resource_key.is_none())
    {
        return Err(CatalogError::Arguments);
    }
    let query = match page.kind {
        ResourceKind::Events => concat!(
            include_str!("resource_catalog/permissions.sql"),
            include_str!("share_policy/consent.sql"),
            include_str!("resource_catalog/events.sql"),
            include_str!("resource_catalog/page.sql")
        ),
        ResourceKind::SystemMetrics | ResourceKind::TrackerMetrics => concat!(
            include_str!("resource_catalog/permissions.sql"),
            include_str!("share_policy/consent.sql"),
            include_str!("resource_catalog/metrics.sql"),
            include_str!("resource_catalog/page.sql")
        ),
        ResourceKind::RawSources => concat!(
            include_str!("resource_catalog/permissions.sql"),
            include_str!("share_policy/consent.sql"),
            include_str!("resource_catalog/raw.sql"),
            include_str!("resource_catalog/page.sql")
        ),
    };
    let mut output = JsonSpool::default();
    let mut versions = BTreeSet::new();
    let mut chunks = sqlx::query_as::<_, (Vec<u8>, Vec<Uuid>)>(query)
        .bind(page.after_key)
        .bind(page.resource_key)
        .bind(i64::from(page.limit))
        .bind(page.detail)
        .bind(model.map(|model| model.destination))
        .bind(owner)
        .bind(health_allowed)
        .bind(denied_events)
        .bind(denied_metrics)
        .bind(page.kind.name())
        .bind(model.and_then(|model| model.channel))
        .fetch(&mut **txn);
    while let Some((chunk, schema_versions)) = chunks.try_next().await? {
        if schema_versions.len() > 4096 {
            return Err(CatalogError::ResultTooLarge);
        }
        versions.extend(schema_versions);
        output
            .write_all(&chunk)
            .map_err(|_| CatalogError::Database)?;
    }
    drop(chunks);
    let document = output.finish().map_err(|_| CatalogError::Database)?;
    Ok((document, versions))
}
