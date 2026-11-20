mod aggregate;
mod entries;
mod evidence;
mod observations;
mod source;

pub(crate) use evidence::LegacyGenericQueryDependency;
pub use evidence::{GENERIC_ANALYSIS, GenericAnalysisEvidence};

use std::{collections::BTreeSet, error::Error, fmt, io::Read};

use ga_archive::json_spool::JsonDocument;
use ga_domain::{
    analysis_spec::{AnalysisOperation, AnalysisSpec},
    model_consent::ModelCategories,
};
use sqlx::{Acquire, Postgres, Transaction};
use uuid::Uuid;

use super::{
    HealthAnalysisError, begin_daily_analysis, model_metric_packs, require_current_projection,
};
use crate::{
    Database, ReadinessError,
    conversation_memory::{AnalysisSubject, analysis_channel_in},
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

pub struct GenericAnalysisReader<'connection> {
    txn: Transaction<'connection, Postgres>,
    audience: GenericAudience,
}

pub struct GenericModelRead {
    pub value: serde_json::Value,
    pub tracker_versions: BTreeSet<Uuid>,
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
    ResultTooLarge,
    Health(HealthAnalysisError),
    Database,
}

impl fmt::Display for GenericAnalysisError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Lookup(message) | Self::Value(message) => message,
            Self::Permission => "Insufficient scope",
            Self::ResultTooLarge => "The generic result is too large",
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
    pub async fn read_model_generic(
        &self,
        context: AnalysisSubject<'_>,
        model: ShareDestination<'_>,
        categories: ModelCategories,
        spec: &AnalysisSpec,
    ) -> Result<GenericModelRead, GenericAnalysisError> {
        let mut txn = begin_daily_analysis(&self.work).await?;
        let channel = analysis_channel_in(&mut txn, context)
            .await
            .map_err(|_| GenericAnalysisError::Database)?
            .ok_or(GenericAnalysisError::Permission)?;
        let destination = ShareDestination::new(DestinationKind::Channel, &channel)
            .map_err(|_| GenericAnalysisError::Database)?;
        let value = model_read_in(
            &mut txn,
            context.binding().owner_id,
            model,
            destination,
            categories,
            spec,
        )
        .await?;
        txn.commit().await?;
        Ok(value)
    }

    /// Hold the source-write fence through authorization and the complete generic read.
    pub async fn generic_analysis_reader(
        &self,
        audience: GenericAudience,
    ) -> Result<GenericAnalysisReader<'static>, GenericAnalysisError> {
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

pub(crate) async fn model_read_in(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    model: ShareDestination<'_>,
    channel: ShareDestination<'_>,
    categories: ModelCategories,
    spec: &AnalysisSpec,
) -> Result<GenericModelRead, GenericAnalysisError> {
    if model.kind() != DestinationKind::Model || channel.kind() != DestinationKind::Channel {
        return Err(GenericAnalysisError::Permission);
    }
    require_current_projection(txn).await?;
    let tracker_versions =
        model_permissions(txn, spec, owner, model, Some(channel), categories).await?;
    let timezone: String = sqlx::query_scalar("SELECT current_setting('TimeZone')")
        .fetch_one(&mut **txn)
        .await?;
    let document = GenericAnalysisReader {
        txn: txn.begin().await?,
        audience: GenericAudience::Owner { read_health: true },
    }
    .read(spec)
    .await?;
    sqlx::query("SELECT set_config('TimeZone', $1, true)")
        .bind(timezone)
        .execute(&mut **txn)
        .await?;
    // The reference compacts at 24,000 characters. Bound the encoded read
    // before decoding even when every character is four UTF-8 bytes.
    const MAX_RESULT_CHARACTERS: usize = 24_000;
    if document.byte_len() > (MAX_RESULT_CHARACTERS * 4) as u64 {
        return Err(GenericAnalysisError::ResultTooLarge);
    }
    let mut bytes = Vec::with_capacity(document.byte_len() as usize);
    document
        .reader()
        .read_to_end(&mut bytes)
        .map_err(|_| GenericAnalysisError::Data)?;
    let encoded = ga_domain::legacy_json::encode(
        &bytes,
        ga_domain::legacy_json::Utf8Layout::Ordered,
        MAX_RESULT_CHARACTERS * 4,
    )
    .map_err(|_| GenericAnalysisError::Encoding)?;
    if std::str::from_utf8(&encoded)
        .map_err(|_| GenericAnalysisError::Encoding)?
        .chars()
        .count()
        > MAX_RESULT_CHARACTERS
    {
        return Err(GenericAnalysisError::ResultTooLarge);
    }
    let value = serde_json::from_slice(&bytes).map_err(|_| GenericAnalysisError::Data)?;
    Ok(GenericModelRead {
        value,
        tracker_versions,
    })
}

impl GenericAnalysisReader<'_> {
    pub async fn read(self, spec: &AnalysisSpec) -> Result<JsonDocument, GenericAnalysisError> {
        match spec.operation() {
            AnalysisOperation::QueryEntries => self.query_entries(spec).await,
            AnalysisOperation::QueryObservations => self.query_observations(spec).await,
            AnalysisOperation::AggregateMetric => self.aggregate_metric(spec).await,
            AnalysisOperation::ComparePeriods => self.compare_periods(spec).await,
            AnalysisOperation::QueryCompleteness => self.query_completeness(spec).await,
            AnalysisOperation::ThresholdSegments => self.threshold_segments(spec).await,
            AnalysisOperation::ThresholdByLocalHour => self.threshold_by_local_hour(spec).await,
        }
    }

    async fn restore_display_timezone(&mut self) -> Result<(), GenericAnalysisError> {
        super::restore_display_timezone(&mut self.txn).await?;
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
        match self.audience {
            GenericAudience::Owner { read_health } => {
                if !read_health && has_source_history(&mut self.txn, spec).await? {
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
                if !current {
                    return Err(GenericAnalysisError::Permission);
                }
                let model = ShareDestination::new(DestinationKind::Model, "model:mcp:local")
                    .map_err(|_| GenericAnalysisError::Database)?;
                model_permissions(
                    &mut self.txn,
                    spec,
                    owner,
                    model,
                    None,
                    ModelCategories::HEALTH_AND_DIARY,
                )
                .await?;
            }
        }
        Ok(())
    }
}

async fn has_source_history(
    txn: &mut Transaction<'_, Postgres>,
    spec: &AnalysisSpec,
) -> Result<bool, GenericAnalysisError> {
    Ok(sqlx::query_scalar(
        "SELECT EXISTS(SELECT FROM metric_observations WHERE metric=$1 AND feature_version='source-plugin-v1')",
    )
    .bind(spec.document()["metric_key"].as_str())
    .fetch_one(&mut **txn)
    .await?)
}

async fn model_permissions(
    txn: &mut Transaction<'_, Postgres>,
    spec: &AnalysisSpec,
    owner: Uuid,
    model: ShareDestination<'_>,
    channel: Option<ShareDestination<'_>>,
    categories: ModelCategories,
) -> Result<BTreeSet<Uuid>, GenericAnalysisError> {
    let document = spec.document();
    let metric = document["metric_key"].as_str();
    let definition = document["definition_key"].as_str();
    let category = if spec.operation() == AnalysisOperation::QueryEntries
        || metric.is_some_and(|key| key.starts_with("user."))
    {
        ModelCategories::DIARY
    } else {
        ModelCategories::HEALTH
    };
    if !categories.covers(category)
        || !onboarding_allows_model(txn, category).await?
        || (has_source_history(txn, spec).await?
            && (!categories.covers(ModelCategories::HEALTH)
                || !onboarding_allows_model(txn, ModelCategories::HEALTH).await?))
    {
        return Err(GenericAnalysisError::Permission);
    }
    let versions: Vec<Uuid> = sqlx::query_scalar(
        "SELECT v.id FROM event_definition_versions v JOIN event_definitions d ON d.id=v.definition_id
         WHERE starts_with($1::text,'user.') AND d.key=$1
         UNION SELECT e.id FROM event_definition_versions e
         JOIN event_metric_mappings m ON m.event_definition_version_id=e.id
         JOIN metric_definition_versions v ON v.id=m.metric_definition_version_id
         JOIN metric_definitions d ON d.id=v.definition_id WHERE starts_with($2::text,'user.') AND d.key=$2",
    )
    .bind(definition)
    .bind(metric)
    .fetch_all(&mut **txn)
    .await?;
    if versions.is_empty()
        && definition
            .into_iter()
            .chain(metric)
            .any(|key| key.starts_with("user."))
    {
        return Err(GenericAnalysisError::Permission);
    }
    for &version_id in &versions {
        let recipients = [
            Some((model, &[ShareCategory::Facts][..])),
            channel.map(|channel| (channel, &[ShareCategory::Schema, ShareCategory::Facts][..])),
        ];
        for (recipient, categories) in recipients.into_iter().flatten() {
            if !allows(
                txn,
                ShareRequirement {
                    version_id,
                    categories,
                },
                recipient,
            )
            .await
            .map_err(|_| GenericAnalysisError::Database)?
            {
                return Err(GenericAnalysisError::Permission);
            }
        }
    }
    if let Some(kind) = definition.and_then(|key| key.strip_prefix("system."))
        && model_disallowed_event_kinds(txn)
            .await
            .map_err(|_| GenericAnalysisError::Database)?
            .contains(&kind)
    {
        return Err(GenericAnalysisError::Permission);
    }
    if let Some(metric) = metric.filter(|key| key.starts_with("system."))
        && !super::read_daily::model_packs_allowed(txn, owner, model_metric_packs(metric)).await?
    {
        return Err(GenericAnalysisError::Permission);
    }
    Ok(versions.into_iter().collect())
}
