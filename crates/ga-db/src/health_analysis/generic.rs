mod aggregate;
mod entries;
mod observations;
mod source;

use std::{error::Error, fmt};

use ga_domain::{
    analysis_spec::{AnalysisOperation, AnalysisSpec},
    model_consent::ModelCategories,
};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{HealthAnalysisError, begin_daily_analysis, model_metric_packs};
use crate::{
    Database, ReadinessError,
    model_gate::onboarding::onboarding_allows_model,
    replay::{GARMIN_PARSER_VERSION, REPLAY_MUTATION_LOCK, projection_pending},
    scenario_packs::model_disallowed_event_kinds,
    share_policy::{DestinationKind, ShareCategory, ShareDestination, ShareRequirement, allows},
};

#[derive(Clone, Copy)]
pub enum GenericAudience {
    Owner { read_health: bool },
    Mcp { owner: Uuid },
}

pub struct GenericAnalysisReader {
    txn: Transaction<'static, Postgres>,
    audience: GenericAudience,
}

#[derive(Debug)]
pub enum GenericAnalysisError {
    Lookup(&'static str),
    Value(&'static str),
    Data,
    Encoding,
    InvalidSnapshot,
    Calendar,
    Calculation(ga_domain::metric_aggregate::MetricAggregateError),
    Permission,
    Health(HealthAnalysisError),
    Database,
}

impl fmt::Display for GenericAnalysisError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Lookup(message) | Self::Value(message) => message,
            Self::Permission => "Insufficient scope",
            Self::Data => "Generic analysis data is incompatible with its database type",
            Self::Encoding => "Metric source cannot be encoded as UTF-8",
            Self::InvalidSnapshot => "Historical event snapshot could not be read",
            Self::Calendar => "Metric window exceeds the datetime calendar",
            Self::Calculation(_) => "Metric values could not be aggregated",
            Self::Health(_) => "Generic analysis storage is unavailable",
            Self::Database => "Generic analysis could not be read",
        })
    }
}
impl Error for GenericAnalysisError {}
impl From<sqlx::Error> for GenericAnalysisError {
    fn from(error: sqlx::Error) -> Self {
        if error
            .as_database_error()
            .and_then(|database| database.code())
            .is_some_and(|code| code.starts_with("22"))
        {
            Self::Data
        } else {
            Self::Database
        }
    }
}
impl From<HealthAnalysisError> for GenericAnalysisError {
    fn from(error: HealthAnalysisError) -> Self {
        Self::Health(error)
    }
}

impl Database {
    /// Hold the source-write fence through authorization and the complete generic read.
    pub async fn generic_analysis_reader(
        &self,
        audience: GenericAudience,
    ) -> Result<GenericAnalysisReader, GenericAnalysisError> {
        let mut txn = begin_daily_analysis(&self.work).await?;
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
            .bind(REPLAY_MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !locked {
            return Err(GenericAnalysisError::Health(HealthAnalysisError::Storage(
                ReadinessError::Unavailable,
            )));
        }
        sqlx::query("SET LOCAL statement_timeout='120s'")
            .execute(&mut *txn)
            .await?;
        let mut reader = GenericAnalysisReader { txn, audience };
        if matches!(audience, GenericAudience::Mcp { .. }) {
            reader.check_replay().await?;
        }
        Ok(reader)
    }
}

impl GenericAnalysisReader {
    async fn restore_display_timezone(&mut self) -> Result<(), GenericAnalysisError> {
        // SQLx starts its connections in UTC. Restore the configured database/role
        // display zone before rendering stored timestamps, as the Python driver does.
        sqlx::query(
            "SELECT set_config('TimeZone', substring(setting FROM 10), true)
             FROM pg_db_role_setting AS defaults
             CROSS JOIN LATERAL unnest(defaults.setconfig) AS config(setting)
             WHERE defaults.setdatabase IN (0,(SELECT oid FROM pg_database WHERE datname=current_database()))
               AND defaults.setrole IN (0,(SELECT oid FROM pg_roles WHERE rolname=session_user))
               AND lower(setting) LIKE 'timezone=%'
             ORDER BY (defaults.setrole<>0) DESC,(defaults.setdatabase<>0) DESC LIMIT 1",
        )
        .execute(&mut *self.txn)
        .await?;
        Ok(())
    }

    async fn check_replay(&mut self) -> Result<(), GenericAnalysisError> {
        if projection_pending(&mut self.txn, GARMIN_PARSER_VERSION).await? {
            return Err(GenericAnalysisError::Health(
                HealthAnalysisError::ReplayPending,
            ));
        }
        Ok(())
    }

    async fn prepare(&mut self, spec: &AnalysisSpec) -> Result<(), GenericAnalysisError> {
        let document = spec.document();
        let metric = document["metric_key"].as_str();
        let definition = document["definition_key"].as_str();
        let source_history:bool=sqlx::query_scalar(
            "SELECT EXISTS(SELECT FROM metric_observations WHERE metric=$1 AND feature_version='source-plugin-v1')")
            .bind(metric).fetch_one(&mut *self.txn).await?;
        match self.audience {
            GenericAudience::Owner { read_health } => {
                if !read_health && source_history {
                    return Err(GenericAnalysisError::Permission);
                }
                self.check_replay().await?;
            }
            GenericAudience::Mcp { owner } => {
                let current: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT FROM people WHERE singleton AND id=$1)",
                )
                .bind(owner)
                .fetch_one(&mut *self.txn)
                .await?;
                let category = if spec.operation() == AnalysisOperation::QueryEntries
                    || metric.is_some_and(|key| key.starts_with("user."))
                {
                    ModelCategories::DIARY
                } else {
                    ModelCategories::HEALTH
                };
                if !current
                    || !onboarding_allows_model(&mut self.txn, category).await?
                    || (source_history
                        && !onboarding_allows_model(&mut self.txn, ModelCategories::HEALTH).await?)
                {
                    return Err(GenericAnalysisError::Permission);
                }
                let versions:Vec<Uuid>=sqlx::query_scalar(
                    "SELECT v.id FROM event_definition_versions v JOIN event_definitions d ON d.id=v.definition_id
                     WHERE starts_with($1::text,'user.') AND d.key=$1
                     UNION SELECT e.id FROM event_definition_versions e
                     JOIN event_metric_mappings m ON m.event_definition_version_id=e.id
                     JOIN metric_definition_versions v ON v.id=m.metric_definition_version_id
                     JOIN metric_definitions d ON d.id=v.definition_id WHERE starts_with($2::text,'user.') AND d.key=$2")
                     .bind(definition).bind(metric).fetch_all(&mut *self.txn).await?;
                let user = definition
                    .into_iter()
                    .chain(metric)
                    .any(|key| key.starts_with("user."));
                if user && versions.is_empty() {
                    return Err(GenericAnalysisError::Permission);
                }
                let model = ShareDestination::new(DestinationKind::Model, "model:mcp:local")
                    .map_err(|_| GenericAnalysisError::Database)?;
                for version_id in versions {
                    if !allows(
                        &mut self.txn,
                        ShareRequirement {
                            version_id,
                            categories: &[ShareCategory::Facts],
                        },
                        model,
                    )
                    .await
                    .map_err(|_| GenericAnalysisError::Database)?
                    {
                        return Err(GenericAnalysisError::Permission);
                    }
                }
                if let Some(kind) = definition.and_then(|key| key.strip_prefix("system."))
                    && model_disallowed_event_kinds(&mut self.txn)
                        .await
                        .map_err(|_| GenericAnalysisError::Database)?
                        .contains(&kind)
                {
                    return Err(GenericAnalysisError::Permission);
                }
                if let Some(metric) = metric.filter(|key| key.starts_with("system.")) {
                    let packs = model_metric_packs(metric);
                    let allowed:bool=sqlx::query_scalar(
                        "SELECT NOT EXISTS(SELECT FROM module_configs WHERE owner_id=$1) OR NOT EXISTS(
                         SELECT FROM unnest($2::text[]) AS required(key) WHERE NOT EXISTS(
                          SELECT FROM module_configs WHERE owner_id=$1 AND pack_key=required.key AND llm_enabled))")
                        .bind(owner).bind(packs).fetch_one(&mut *self.txn).await?;
                    if !allowed {
                        return Err(GenericAnalysisError::Permission);
                    }
                }
            }
        }
        Ok(())
    }
}
