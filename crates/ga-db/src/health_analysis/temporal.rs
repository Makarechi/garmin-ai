use std::{
    collections::{BTreeMap, BTreeSet},
    time::Duration,
};

use futures_util::TryStreamExt;
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{FromRow, Row, postgres::PgRow};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{HealthAnalysisError, begin_verified_analysis};
use crate::Database;

pub(crate) const PRE_EVENT_FEATURE_VERSION: &str = "pre-event-v1";

pub type TemporalFeatures = BTreeMap<StoredInstant, BTreeMap<String, Value>>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TemporalPurpose {
    Retrospective,
    AsKnown,
}

impl TemporalPurpose {
    fn label(self) -> &'static str {
        match self {
            Self::Retrospective => "retrospective",
            Self::AsKnown => "as_known",
        }
    }
}

struct Feature {
    event: Option<OffsetDateTime>,
    metric: Option<String>,
    value: Option<f64>,
    observation: Option<Uuid>,
    source: Option<Uuid>,
    observed: Option<OffsetDateTime>,
    ingested: Option<OffsetDateTime>,
    start: Option<OffsetDateTime>,
    timezone: Option<String>,
    day: Option<Date>,
}

impl FromRow<'_, PgRow> for Feature {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            event: row.try_get("clock")?,
            metric: row.try_get("metric")?,
            value: row.try_get("value")?,
            observation: row.try_get("id")?,
            source: row.try_get("source_ref")?,
            observed: row.try_get("observed_at")?,
            ingested: row.try_get("ingested_at")?,
            start: row.try_get("effective_start")?,
            timezone: row.try_get("timezone")?,
            day: row.try_get("source_calendar_date")?,
        })
    }
}

impl Feature {
    fn document(
        &self,
        event: StoredInstant,
        cutoff: StoredInstant,
        purpose: TemporalPurpose,
    ) -> Result<Value, HealthAnalysisError> {
        let instant = |value: Option<OffsetDateTime>| -> Result<_, HealthAnalysisError> {
            value
                .map(|value| StoredInstant::try_from(value).map(|value| value.to_string()))
                .transpose()
                .map_err(|_| HealthAnalysisError::Database)
        };
        Ok(json!({
            "value": self.value,
            "event_cutoff": event.to_string(),
            "knowledge_cutoff": cutoff.to_string(),
            "purpose": purpose.label(),
            "feature_version": PRE_EVENT_FEATURE_VERSION,
            "observation_id": self.observation.map(|id| id.to_string()),
            "source_ref": self.source.map(|id| id.to_string()),
            "observed_at": instant(self.observed)?,
            "ingested_at": instant(self.ingested)?,
            "effective_start": instant(self.start)?,
            "interpretation_timezone": self.timezone,
            "source_calendar_date": self.day.map(|day| day.to_string()),
            "quality": if self.observation.is_some() { "observed" } else { "unknown" },
        }))
    }
}

impl Database {
    /// Find verified evidence strictly before each event and known by the requested cutoff.
    pub async fn temporal_features(
        &self,
        metrics: &[&str],
        events: &[StoredInstant],
        cutoff: StoredInstant,
        purpose: TemporalPurpose,
    ) -> Result<TemporalFeatures, HealthAnalysisError> {
        let events: BTreeSet<_> = events.iter().copied().collect();
        let Some(&earliest) = events.first() else {
            return Ok(BTreeMap::new());
        };
        if purpose == TemporalPurpose::AsKnown && cutoff > earliest {
            return Err(HealthAnalysisError::InvalidKnowledgeCutoff);
        }
        let latest = events.last().copied().unwrap_or(earliest);
        let mut result: TemporalFeatures = events
            .iter()
            .map(|event| (*event, BTreeMap::new()))
            .collect();
        let metrics: BTreeSet<_> = metrics.iter().copied().collect();
        if metrics.is_empty() {
            return Ok(result);
        }
        let clocks: Vec<_> = events.into_iter().map(OffsetDateTime::from).collect();
        let metrics: Vec<_> = metrics.into_iter().map(str::to_owned).collect();
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = begin_verified_analysis(&self.work).await?;
            {
                let mut rows = sqlx::query_as::<_, Feature>(include_str!("temporal/features.sql"))
                    .bind(&metrics)
                    .bind(&clocks)
                    .bind(OffsetDateTime::from(earliest))
                    .bind(OffsetDateTime::from(latest))
                    .bind(OffsetDateTime::from(cutoff))
                    .bind(PRE_EVENT_FEATURE_VERSION)
                    .fetch(&mut *txn);
                while let Some(row) = rows.try_next().await? {
                    let Some(event) = row.event else {
                        return Err(HealthAnalysisError::TemporalVersionLimit);
                    };
                    let event = StoredInstant::try_from(event)
                        .map_err(|_| HealthAnalysisError::Database)?;
                    let metric = row
                        .metric
                        .as_ref()
                        .ok_or(HealthAnalysisError::Database)?
                        .to_owned();
                    result
                        .get_mut(&event)
                        .ok_or(HealthAnalysisError::Database)?
                        .insert(metric, row.document(event, cutoff, purpose)?);
                }
            }
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }
}
