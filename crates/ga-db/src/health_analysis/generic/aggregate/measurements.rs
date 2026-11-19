use futures_util::TryStreamExt;
use ga_domain::sha256_hex;
use serde_json::{Value, json};
use sqlx::{Row, postgres::PgRow};
use uuid::Uuid;

use super::{
    interval,
    rows::{AggregateRow, ascii_json, floating},
};
use crate::health_analysis::generic::{
    GenericAnalysisError, GenericAnalysisReader, source::SourceSelection,
};

pub(super) type MeasurementIdentity = (i64, String, String);

pub(super) struct MeasurementQuery<'a> {
    pub contract: Uuid,
    pub start: Option<i64>,
    pub end: i64,
    pub cutoff: i64,
    pub source: Option<&'a str>,
    pub limit: i64,
    pub descending: bool,
    pub include_invalid: bool,
    pub retain_evidence: bool,
}

pub(super) struct MeasurementSnapshot {
    pub id: Option<Uuid>,
    pub observed: i64,
    pub metric: String,
    pub source: String,
    pub value: Value,
    pub source_ref: Option<Uuid>,
    pub quality: String,
    pub contract: Option<Uuid>,
    pub ingested: i64,
    pub ingested_display: String,
    pub deleted: bool,
    pub evidence: Option<(String, String)>,
}

pub(super) struct SupplementalMeasurement {
    pub identity: MeasurementIdentity,
    pub historical: bool,
    pub row: AggregateRow,
}

impl MeasurementSnapshot {
    fn decode(row: PgRow, retain_evidence: bool) -> Result<Self, GenericAnalysisError> {
        let id: Option<Uuid> = row.try_get("id")?;
        let observed = row.try_get("observed")?;
        let metric: String = row.try_get("metric")?;
        let source: String = row.try_get("source")?;
        let value = floating(row.try_get("value")?)?;
        let source_ref: Option<Uuid> = row.try_get("source_ref")?;
        let quality: String = row.try_get("quality")?;
        let ingested_display: String = row.try_get("ingested_display")?;
        let evidence = if retain_evidence {
            let observed_display: String = row.try_get("observed_display")?;
            let reference = format!(
                "measurement:{}",
                ascii_json(&json!([observed_display, metric, source]))?
            );
            let token = match id {
                Some(id) => id.to_string(),
                None => {
                    let unit: String = row.try_get("unit")?;
                    let details: String = row.try_get("details")?;
                    let mut payload = json!({
                        "value":value,"unit":unit,"source_ref":source_ref.map(|id|id.to_string()),
                        "quality":quality,"details":null,"ingested_at":ingested_display,
                    });
                    payload["details"] = serde_json::from_str(&details)
                        .map_err(|_| GenericAnalysisError::Database)?;
                    format!("legacy:{}", sha256_hex(ascii_json(&payload)?.as_bytes()))
                }
            };
            Some((reference, token))
        } else {
            None
        };
        Ok(Self {
            id,
            observed,
            metric,
            source,
            value,
            source_ref,
            quality,
            contract: row.try_get("metric_definition_version_id")?,
            ingested: row.try_get("ingested")?,
            ingested_display,
            deleted: row.try_get("deleted")?,
            evidence,
        })
    }

    pub fn identity(&self) -> MeasurementIdentity {
        (self.observed, self.metric.clone(), self.source.clone())
    }

    pub fn into_aggregate(self) -> AggregateRow {
        AggregateRow {
            id: format!(
                "measurement-revision:{}",
                self.id
                    .map(|id| id.to_string())
                    .unwrap_or_else(|| "legacy".to_owned())
            ),
            value: self.value,
            observed: self.observed,
            effective_start: Some(self.observed),
            effective_end: None,
            recorded: self.ingested,
            ingested: self.ingested,
            ingested_display: self.ingested_display,
            sequence: 0,
            source: format!("measurement:{}", self.source),
            source_ref: self.source_ref,
            source_entry: None,
            projection_version: None,
            coverage: None,
        }
    }
}

impl GenericAnalysisReader {
    pub(super) async fn aggregate_measurement_fallback(
        &mut self,
        contract: Uuid,
        window: &super::AggregateWindow,
        source: &SourceSelection,
        prior: bool,
    ) -> Result<Vec<SupplementalMeasurement>, GenericAnalysisError> {
        let start = if prior {
            window.start
        } else {
            window.measurement_start
        };
        let mut query = sqlx::query(include_str!("measurement_fallback.sql"))
            .bind(contract)
            .bind(interval(start))
            .bind(interval(window.end))
            .bind(interval(window.cutoff))
            .bind(source.mode)
            .bind(source.measurement.as_deref())
            .bind(prior)
            .fetch(&mut *self.txn);
        let mut measurements = Vec::new();
        while let Some(row) = query.try_next().await? {
            let observed = row.try_get("observed")?;
            let metric: String = row.try_get("metric")?;
            let source: String = row.try_get("source")?;
            let ingested = row.try_get("ingested")?;
            measurements.push(SupplementalMeasurement {
                identity: (observed, metric, source.clone()),
                historical: row.try_get("historical")?,
                row: AggregateRow {
                    id: row.try_get("id")?,
                    value: floating(row.try_get("value")?)?,
                    observed,
                    effective_start: Some(observed),
                    effective_end: None,
                    recorded: ingested,
                    ingested,
                    ingested_display: row.try_get("ingested_display")?,
                    sequence: 0,
                    source: format!("measurement:{source}"),
                    source_ref: row.try_get("source_ref")?,
                    source_entry: None,
                    projection_version: None,
                    coverage: None,
                },
            });
        }
        Ok(measurements)
    }

    pub(super) async fn aggregate_measurement_snapshot(
        &mut self,
        options: MeasurementQuery<'_>,
    ) -> Result<Vec<MeasurementSnapshot>, GenericAnalysisError> {
        let mut query = sqlx::query(include_str!("measurement_snapshot.sql"))
            .bind(options.contract)
            .bind(options.start.map(interval))
            .bind(interval(options.end))
            .bind(interval(options.cutoff))
            .bind(options.source)
            .bind(options.limit)
            .bind(options.descending)
            .bind(options.include_invalid)
            .bind(options.retain_evidence)
            .fetch(&mut *self.txn);
        let mut snapshots = Vec::new();
        while let Some(row) = query.try_next().await? {
            snapshots.push(MeasurementSnapshot::decode(row, options.retain_evidence)?);
        }
        Ok(snapshots)
    }
}
