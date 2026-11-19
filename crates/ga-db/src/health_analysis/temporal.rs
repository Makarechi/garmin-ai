use std::{
    collections::{BTreeMap, BTreeSet},
    time::Duration,
};

use futures_util::TryStreamExt;
use ga_domain::timestamp::{OffsetTimestamp, StoredInstant, regional_clock_instant};
use serde_json::{Value, json};
use sqlx::{FromRow, Row, postgres::PgRow};
use time::{Date, OffsetDateTime, PrimitiveDateTime};
use uuid::Uuid;

use super::{HealthAnalysisError, begin_verified_analysis, restore_display_timezone};
use crate::Database;

pub(crate) const PRE_EVENT_FEATURE_VERSION: &str = "pre-event-v1";

pub type TemporalFeatures = BTreeMap<StoredInstant, BTreeMap<String, Value>>;
pub type TemporalOffsetFeatures = BTreeMap<OffsetDateTime, BTreeMap<String, Value>>;

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum TemporalClock {
    FixedOffset,
    DatabaseTimezone,
}

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
    observed: Option<String>,
    ingested: Option<String>,
    start: Option<String>,
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
        event: OffsetDateTime,
        cutoff: OffsetDateTime,
        purpose: TemporalPurpose,
    ) -> Value {
        json!({
            "value": self.value,
            "event_cutoff": OffsetTimestamp(event).to_string(),
            "knowledge_cutoff": OffsetTimestamp(cutoff).to_string(),
            "purpose": purpose.label(),
            "feature_version": PRE_EVENT_FEATURE_VERSION,
            "observation_id": self.observation.map(|id| id.to_string()),
            "source_ref": self.source.map(|id| id.to_string()),
            "observed_at": self.observed,
            "ingested_at": self.ingested,
            "effective_start": self.start,
            "interpretation_timezone": self.timezone,
            "source_calendar_date": self.day.map(|day| day.to_string()),
            "quality": if self.observation.is_some() { "observed" } else { "unknown" },
        })
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
        let events: Vec<_> = events.iter().copied().map(OffsetDateTime::from).collect();
        self.temporal_features_with_offsets(metrics, &events, cutoff.into(), purpose)
            .await?
            .into_iter()
            .map(|(clock, values)| {
                StoredInstant::try_from(clock)
                    .map(|clock| (clock, values))
                    .map_err(|_| HealthAnalysisError::Database)
            })
            .collect()
    }

    /// Preserve the original input offsets in temporal evidence and equal-instant keys.
    pub async fn temporal_features_with_offsets(
        &self,
        metrics: &[&str],
        events: &[OffsetDateTime],
        cutoff: OffsetDateTime,
        purpose: TemporalPurpose,
    ) -> Result<TemporalOffsetFeatures, HealthAnalysisError> {
        for &clock in events.iter().chain(std::iter::once(&cutoff)) {
            StoredInstant::try_from(clock)
                .map_err(|_| HealthAnalysisError::InvalidTemporalTimestamp)?;
        }
        if events.is_empty() {
            return Ok(BTreeMap::new());
        }
        if purpose == TemporalPurpose::AsKnown && events.iter().any(|event| cutoff > *event) {
            return Err(HealthAnalysisError::InvalidKnowledgeCutoff);
        }
        let clocks = event_clocks(events);
        lower_bound(
            *clocks
                .first_key_value()
                .ok_or(HealthAnalysisError::Database)?
                .0,
        )?;
        if metrics.is_empty() {
            return Ok(clocks
                .into_keys()
                .map(|event| (event, BTreeMap::new()))
                .collect());
        }
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = begin_verified_analysis(&self.work).await?;
            let result = temporal_features_in(
                &mut txn,
                metrics,
                events,
                cutoff,
                purpose,
                TemporalClock::FixedOffset,
            )
            .await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }
}

fn event_clocks(events: &[OffsetDateTime]) -> BTreeMap<OffsetDateTime, OffsetDateTime> {
    let mut clocks = BTreeMap::new();
    for &event in events {
        // Python's dict retains the first equal key and the last supplied value.
        clocks
            .entry(event)
            .and_modify(|display| *display = event)
            .or_insert(event);
    }
    clocks
}

pub(super) fn local_event_clocks(
    events: &[OffsetDateTime],
) -> BTreeMap<PrimitiveDateTime, (OffsetDateTime, OffsetDateTime)> {
    let mut clocks = BTreeMap::new();
    for &event in events {
        clocks
            .entry(event.date().with_time(event.time()))
            .and_modify(|(_, display)| *display = event)
            .or_insert((event, event));
    }
    clocks
}

fn lower_bound(event: OffsetDateTime) -> Result<OffsetDateTime, HealthAnalysisError> {
    event
        .checked_sub(time::Duration::hours(24))
        .filter(|time| (1..=9999).contains(&time.year()))
        .ok_or(HealthAnalysisError::TemporalCalendarOverflow)
}

pub(super) async fn restore_clock_timezone(
    txn: &mut sqlx::Transaction<'_, sqlx::Postgres>,
) -> Result<(), sqlx::Error> {
    restore_display_timezone(txn).await?;
    let timezone: String = sqlx::query_scalar("SELECT current_setting('TimeZone')")
        .fetch_one(&mut **txn)
        .await?;
    if !ga_domain::timestamp::source_timezone_available(&timezone) {
        // The reference driver falls back to UTC for PostgreSQL-only timezone names.
        sqlx::query("SET LOCAL TimeZone='UTC'")
            .execute(&mut **txn)
            .await?;
    }
    Ok(())
}

pub(super) async fn temporal_features_in(
    txn: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    metrics: &[&str],
    events: &[OffsetDateTime],
    cutoff: OffsetDateTime,
    purpose: TemporalPurpose,
    clock_source: TemporalClock,
) -> Result<TemporalOffsetFeatures, HealthAnalysisError> {
    let ordered: Vec<_> = match clock_source {
        TemporalClock::FixedOffset => event_clocks(events).into_iter().collect(),
        TemporalClock::DatabaseTimezone => local_event_clocks(events).into_values().collect(),
    };
    let Some(&(earliest, _)) = ordered.first() else {
        return Ok(BTreeMap::new());
    };
    if purpose == TemporalPurpose::AsKnown && events.iter().any(|event| cutoff > *event) {
        return Err(HealthAnalysisError::InvalidKnowledgeCutoff);
    }
    let latest = ordered.last().map(|(clock, _)| *clock).unwrap_or(earliest);
    let mut lower = lower_bound(earliest)?;
    restore_clock_timezone(txn).await?;
    if clock_source == TemporalClock::DatabaseTimezone {
        let timezone: String = sqlx::query_scalar("SELECT current_setting('TimeZone')")
            .fetch_one(&mut **txn)
            .await?;
        lower = regional_clock_instant(lower.date().with_time(lower.time()), &timezone)
            .ok_or(HealthAnalysisError::Database)?;
    }
    let events: BTreeMap<_, _> = ordered.into_iter().collect();
    let mut result: TemporalOffsetFeatures = events
        .keys()
        .map(|event| (*event, BTreeMap::new()))
        .collect();
    let metrics: BTreeSet<_> = metrics.iter().copied().collect();
    if metrics.is_empty() {
        return Ok(result);
    }
    let clocks: Vec<_> = events.keys().copied().collect();
    let metrics: Vec<_> = metrics.into_iter().map(str::to_owned).collect();
    let mut rows = sqlx::query_as::<_, Feature>(include_str!("temporal/features.sql"))
        .bind(&metrics)
        .bind(&clocks)
        .bind(lower)
        .bind(latest)
        .bind(cutoff)
        .bind(PRE_EVENT_FEATURE_VERSION)
        .bind(clock_source == TemporalClock::DatabaseTimezone)
        .fetch(&mut **txn);
    while let Some(row) = rows.try_next().await? {
        let Some(event) = row.event else {
            return Err(HealthAnalysisError::TemporalVersionLimit);
        };
        let metric = row
            .metric
            .as_ref()
            .ok_or(HealthAnalysisError::Database)?
            .to_owned();
        result
            .get_mut(&event)
            .ok_or(HealthAnalysisError::Database)?
            .insert(
                metric,
                row.document(
                    *events.get(&event).ok_or(HealthAnalysisError::Database)?,
                    cutoff,
                    purpose,
                ),
            );
    }
    Ok(result)
}
