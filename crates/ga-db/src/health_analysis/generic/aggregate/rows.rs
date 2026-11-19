use futures_util::TryStreamExt;
use ga_domain::legacy_json::{Utf8Layout, encode_ascii};
use serde_json::{Value, json};
use sqlx::{Row, postgres::PgRow};
use uuid::Uuid;

use super::{AggregateContract, AggregateWindow, interval};
use crate::health_analysis::generic::{
    GenericAnalysisError, GenericAnalysisReader, source::SourceSelection,
};

pub(super) struct AggregateRow {
    pub id: String,
    pub value: Value,
    pub observed: i64,
    pub effective_start: Option<i64>,
    pub effective_end: Option<i64>,
    pub recorded: i64,
    pub ingested: i64,
    pub ingested_display: String,
    pub sequence: i32,
    pub source: String,
    pub source_ref: Option<Uuid>,
    pub source_entry: Option<Uuid>,
    pub projection_version: Option<i32>,
    pub coverage: Option<f64>,
}

pub(super) fn floating(value: f64) -> Result<Value, GenericAnalysisError> {
    serde_json::Number::from_f64(value)
        .map(Value::Number)
        .ok_or(GenericAnalysisError::Calculation(
            ga_domain::metric_aggregate::MetricAggregateError::Nonfinite,
        ))
}

pub(super) fn ascii_json(value: &Value) -> Result<String, GenericAnalysisError> {
    let bytes = serde_json::to_vec(value).map_err(|_| GenericAnalysisError::Database)?;
    encode_ascii(&bytes, Utf8Layout::Compact, bytes.len())
        .map_err(|_| GenericAnalysisError::Database)
}

impl AggregateRow {
    fn observation(row: PgRow) -> Result<Self, GenericAnalysisError> {
        let numeric: Option<f64> = row.try_get("value_number")?;
        let text: Option<String> = row.try_get("value_text")?;
        let boolean: Option<bool> = row.try_get("value_boolean")?;
        let value = match (numeric, text, boolean) {
            (Some(value), _, _) => floating(value)?,
            (_, Some(value), _) => Value::String(value),
            (_, _, Some(value)) => Value::Bool(value),
            _ => Value::Null,
        };
        let source_entry: Option<Uuid> = row.try_get("source_entry_id")?;
        let source = if source_entry.is_some() {
            "event".to_owned()
        } else {
            let account: Option<String> = row.try_get("account")?;
            let device: Option<String> = row.try_get("device")?;
            format!("observation:{}", ascii_json(&json!([account, device]))?)
        };
        let ingested = row.try_get("ingested")?;
        Ok(Self {
            id: row.try_get("id")?,
            value,
            observed: row.try_get("observed")?,
            effective_start: row.try_get("effective_start")?,
            effective_end: row.try_get("effective_end")?,
            recorded: row
                .try_get::<Option<i64>, _>("recorded")?
                .unwrap_or(ingested),
            ingested,
            ingested_display: row.try_get("ingested_display")?,
            sequence: row.try_get("sequence")?,
            source,
            source_ref: row.try_get("source_ref")?,
            source_entry,
            projection_version: row.try_get("projection_version")?,
            coverage: row.try_get("coverage")?,
        })
    }
}

impl GenericAnalysisReader {
    pub(super) async fn aggregate_observations(
        &mut self,
        contract: &AggregateContract,
        window: &AggregateWindow,
        source: &SourceSelection,
        prior: bool,
    ) -> Result<Vec<AggregateRow>, GenericAnalysisError> {
        let mut query = sqlx::query(include_str!("observations.sql"))
            .bind(contract.id)
            .bind(interval(window.start))
            .bind(interval(window.end))
            .bind(interval(window.cutoff))
            .bind(interval(window.predecessor_start))
            .bind(source.mode)
            .bind(source.account.as_deref())
            .bind(source.device.as_deref())
            .bind(&contract.value_kind)
            .bind(&contract.time_semantics)
            .bind(contract.value_kind == "cumulative_counter" && contract.method == "delta")
            .bind(prior)
            .fetch(&mut *self.txn);
        let mut rows = Vec::new();
        while let Some(row) = query.try_next().await? {
            rows.push(AggregateRow::observation(row)?);
        }
        Ok(rows)
    }
}
