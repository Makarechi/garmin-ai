use std::{collections::BTreeSet, error::Error, fmt, io::Write, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::model_consent::ModelCategories;
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

#[derive(Debug)]
pub enum CatalogError {
    Arguments,
    ModelConsent,
    Health(HealthAnalysisError),
    Database,
}

impl fmt::Display for CatalogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Arguments => "Resource catalog arguments are invalid",
            Self::ModelConsent => "The resource model category is disabled",
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
            if !matches!(page.kind, ResourceKind::Events) {
                let locked: bool =
                    sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
                        .bind(REPLAY_MUTATION_LOCK)
                        .fetch_one(&mut *txn)
                        .await?;
                if !locked {
                    return Err(CatalogError::Health(HealthAnalysisError::Storage(
                        ReadinessError::Unavailable,
                    )));
                }
                if page.kind.health() && projection_pending(&mut txn, GARMIN_PARSER_VERSION).await?
                {
                    return Err(CatalogError::Health(HealthAnalysisError::ReplayPending));
                }
            }
            let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
                .fetch_one(&mut *txn)
                .await?;
            let mut health_allowed = health_scope;
            let mut denied_events = Vec::new();
            let mut denied_metrics = Vec::new();
            if let Some(model_owner) = model_owner {
                let category = if page.kind.health() {
                    ModelCategories::HEALTH
                } else {
                    ModelCategories::DIARY
                };
                if model_owner != owner || !onboarding_allows_model(&mut txn, category).await? {
                    return Err(CatalogError::ModelConsent);
                }
                health_allowed &=
                    onboarding_allows_model(&mut txn, ModelCategories::HEALTH).await?;
                denied_events = model_disallowed_event_kinds(&mut txn)
                    .await
                    .map_err(|_| CatalogError::Database)?;
                let packs: Vec<(String, bool)> = sqlx::query_as(
                    "SELECT pack_key,llm_enabled FROM module_configs WHERE owner_id=$1",
                )
                .bind(owner)
                .fetch_all(&mut *txn)
                .await?;
                if !packs.is_empty() && matches!(page.kind, ResourceKind::SystemMetrics) {
                    let enabled: BTreeSet<_> = packs
                        .iter()
                        .filter_map(|(key, on)| on.then_some(key.as_str()))
                        .collect();
                    let keys: Vec<String> = sqlx::query_scalar(
                        "SELECT key FROM metric_definitions WHERE namespace='system'",
                    )
                    .fetch_all(&mut *txn)
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
            let mut chunks = sqlx::query_scalar::<_, Vec<u8>>(query)
                .bind(page.after_key)
                .bind(page.resource_key)
                .bind(i64::from(page.limit))
                .bind(page.detail)
                .bind(model_owner.map(|_| "model:mcp:local"))
                .bind(owner)
                .bind(health_allowed)
                .bind(denied_events)
                .bind(denied_metrics)
                .bind(page.kind.name())
                .fetch(&mut *txn);
            while let Some(chunk) = chunks.try_next().await? {
                output
                    .write_all(&chunk)
                    .map_err(|_| CatalogError::Database)?;
            }
            drop(chunks);
            txn.commit().await?;
            output.finish().map_err(|_| CatalogError::Database)
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| CatalogError::Database)?
    }
}
