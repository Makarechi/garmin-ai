use std::{error::Error, fmt, time::Duration};

use futures_util::TryStreamExt;
use ga_domain::legacy_metrics::legacy_metric_contracts;
use serde_json::{Value, json};
use sqlx::{FromRow, Postgres, Row, Transaction, postgres::PgRow};
use time::{OffsetDateTime, UtcOffset};

use super::{DailyHealthReader, HealthAnalysisError, check_calendar_bounds, isoformat};

#[derive(Debug)]
pub enum SeriesReadError {
    Invalid,
    SampleLimit,
    Analysis(HealthAnalysisError),
}

impl fmt::Display for SeriesReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid => f.write_str("Invalid measurement series arguments"),
            Self::SampleLimit => {
                f.write_str("Series exceeds 100000 samples; request a shorter time range")
            }
            Self::Analysis(error) => error.fmt(f),
        }
    }
}
impl Error for SeriesReadError {}
impl From<sqlx::Error> for SeriesReadError {
    fn from(_: sqlx::Error) -> Self {
        Self::Analysis(HealthAnalysisError::Database)
    }
}
impl From<HealthAnalysisError> for SeriesReadError {
    fn from(error: HealthAnalysisError) -> Self {
        Self::Analysis(error)
    }
}

struct SeriesRow {
    over_limit: bool,
    bucket: Option<OffsetDateTime>,
    document: Option<String>,
}
impl FromRow<'_, PgRow> for SeriesRow {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            over_limit: row.try_get("over_limit")?,
            bucket: row.try_get("bucket")?,
            document: row.try_get("document")?,
        })
    }
}

impl DailyHealthReader {
    /// Aggregate observed measurements by canonical source and coverage in the verified read snapshot.
    pub async fn metric_series(
        mut self,
        metric: &str,
        start: OffsetDateTime,
        end: OffsetDateTime,
        minutes: u16,
    ) -> Result<Value, SeriesReadError> {
        let work = async {
            sqlx::query("SET LOCAL statement_timeout = '1500ms'")
                .execute(&mut *self.0)
                .await?;
            let result = metric_series_in(&mut self.0, metric, start, end, minutes).await?;
            self.0.commit().await?;
            Ok(result)
        };
        tokio::time::timeout(Duration::from_millis(1800), work)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
}

pub(super) async fn metric_series_in(
    txn: &mut Transaction<'_, Postgres>,
    metric: &str,
    start: OffsetDateTime,
    end: OffsetDateTime,
    minutes: u16,
) -> Result<Value, SeriesReadError> {
    check_calendar_bounds(
        start.to_offset(UtcOffset::UTC).date(),
        end.to_offset(UtcOffset::UTC).date(),
    )
    .map_err(|_| SeriesReadError::Invalid)?;
    if start >= end || end - start > time::Duration::days(366) || !(1..=1440).contains(&minutes) {
        return Err(SeriesReadError::Invalid);
    }
    series_in(
        txn,
        metric,
        start,
        end,
        minutes,
        2000,
        OffsetDateTime::UNIX_EPOCH,
    )
    .await
}

pub(super) async fn series_in(
    txn: &mut Transaction<'_, Postgres>,
    metric: &str,
    start: OffsetDateTime,
    end: OffsetDateTime,
    minutes: u16,
    limit: u16,
    origin: OffsetDateTime,
) -> Result<Value, SeriesReadError> {
    let contract = legacy_metric_contracts()
        .remove(metric)
        .filter(|contract| contract["kind"] != "daily_summary")
        .ok_or(SeriesReadError::Invalid)?;
    let unit = contract["unit"].as_str().ok_or(SeriesReadError::Invalid)?;
    let aggregation = contract["aggregation"]
        .as_str()
        .ok_or(SeriesReadError::Invalid)?;
    let minimum = contract["minimum"]
        .as_f64()
        .ok_or(SeriesReadError::Invalid)?;
    let maximum = contract["maximum"].as_f64();
    let gap = contract["max_gap_seconds"]
        .as_i64()
        .and_then(|seconds| i32::try_from(seconds).ok())
        .ok_or(SeriesReadError::Invalid)?;
    let mut results = sqlx::query_as::<_, SeriesRow>(include_str!("series.sql"))
        .bind(metric)
        .bind(unit)
        .bind(minimum)
        .bind(maximum)
        .bind(start)
        .bind(end)
        .bind(f64::from(gap))
        .bind(f64::from(minutes) * 60.0)
        .bind(origin)
        .bind(aggregation == "time_weighted_mean")
        .bind(i64::from(limit) + 1)
        .fetch(&mut **txn);
    let mut rows = Vec::new();
    let mut truncated = false;
    while let Some(row) = results.try_next().await? {
        if row.over_limit {
            return Err(SeriesReadError::SampleLimit);
        }
        if rows.len() == usize::from(limit) {
            truncated = true;
            break;
        }
        let mut document: Value =
            serde_json::from_str(row.document.as_deref().ok_or(SeriesReadError::Invalid)?)
                .map_err(|_| SeriesReadError::Analysis(HealthAnalysisError::Database))?;
        document["ts"] = json!(isoformat(row.bucket.ok_or(SeriesReadError::Invalid)?));
        rows.push(document);
    }
    drop(results);
    let mut document = json!({
        "metric":metric,"contract":contract,"start":isoformat(start),"end":isoformat(end),
        "bucket_minutes":minutes,"truncated":truncated,
        "limitations":[
            "Sources are separate; overlapping providers are not added together",
            "Gauges use bounded left-hold intervals and require 80% coverage",
            "Increment buckets are selected by source interval start",
        ],
    });
    document["rows"] = Value::Array(rows);
    Ok(document)
}
