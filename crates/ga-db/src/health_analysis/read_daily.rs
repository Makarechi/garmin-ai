use futures_util::TryStreamExt;
use ga_domain::{legacy_metrics::legacy_metric_contracts, timestamp::StoredInstant};
use serde_json::{Value, json};
use time::{Date, OffsetDateTime};

use super::{HealthAnalysisError, HealthPeriod, begin_daily_analysis};
use crate::Database;

const METRICS: &[&str] = &[
    "active_calories",
    "awake_seconds",
    "body_battery_charged",
    "body_battery_drained",
    "body_battery_high",
    "body_battery_low",
    "deep_seconds",
    "hrv_baseline_high",
    "hrv_baseline_low",
    "hrv_nightly_avg",
    "hrv_status",
    "hrv_weekly_avg",
    "hydration_ml",
    "intensity_minutes",
    "light_seconds",
    "recovery_time_minutes",
    "rem_seconds",
    "resting_hr",
    "sleep_score",
    "sleep_seconds",
    "steps",
    "stress_avg",
    "stress_max",
    "training_readiness_score",
    "training_status",
    "vo2max",
];
const DAILY_ROWS: &str = "SELECT row_to_json(health_days)::text, updated_at FROM health_days
     WHERE day BETWEEN $1 AND $2 ORDER BY day";

impl Database {
    /// Return recorded summaries for this inclusive calendar range.
    pub async fn read_health_range(
        &self,
        period: HealthPeriod,
    ) -> Result<Value, HealthAnalysisError> {
        let mut txn = begin_daily_analysis(&self.work).await?;
        let mut rows = Vec::new();
        {
            let mut stream = sqlx::query_as::<_, (String, OffsetDateTime)>(DAILY_ROWS)
                .bind(period.start)
                .bind(period.end)
                .fetch(&mut *txn);
            while let Some((document, updated_at)) = stream.try_next().await? {
                rows.push(health_day(&document, updated_at)?);
            }
        }
        txn.commit().await?;
        let mut response = json!({
            "start": period.start.to_string(),
            "end": period.end.to_string(),
            "days_available": rows.len(),
            "days_requested": period.days(),
        });
        response["rows"] = Value::Array(rows);
        Ok(response)
    }

    /// Show the stored calendar day, including missing metrics and the separate hydration contract.
    pub async fn read_health_snapshot(&self, day: Date) -> Result<Value, HealthAnalysisError> {
        if !(1..=9999).contains(&day.year()) {
            return Err(HealthAnalysisError::DateRange);
        }
        let mut txn = begin_daily_analysis(&self.work).await?;
        let row = sqlx::query_as::<_, (String, OffsetDateTime)>(DAILY_ROWS)
            .bind(day)
            .bind(day)
            .fetch_optional(&mut *txn)
            .await?;
        txn.commit().await?;
        let values = row
            .map(|(document, updated_at)| health_day(&document, updated_at))
            .transpose()?;
        let missing: Vec<&str> = METRICS
            .iter()
            .copied()
            .filter(|field| {
                values
                    .as_ref()
                    .is_none_or(|values| values[*field].is_null())
            })
            .collect();
        Ok(json!({
            "date": day.to_string(),
            "available": values.is_some(),
            "values": values,
            "time_semantics": "daily_summary; updated_at is ingestion time, not measurement time",
            "usable_for_current_state": false,
            "metric_contracts": legacy_metric_contracts(),
            "hydration_sources": "Garmin daily total; manual diary water is separate and not summed",
            "missing_metrics": missing,
        }))
    }
}

fn health_day(document: &str, updated_at: OffsetDateTime) -> Result<Value, HealthAnalysisError> {
    let mut row: Value =
        serde_json::from_str(document).map_err(|_| HealthAnalysisError::Database)?;
    let values = row.as_object_mut().ok_or(HealthAnalysisError::Database)?;
    values.retain(|key, _| {
        matches!(key.as_str(), "day" | "sources") || METRICS.contains(&key.as_str())
    });
    for field in METRICS {
        let value = values
            .get_mut(*field)
            .ok_or(HealthAnalysisError::Database)?;
        if !value.is_null() && !matches!(*field, "steps" | "hrv_status" | "training_status") {
            let number = value
                .as_f64()
                .filter(|value| value.is_finite())
                .ok_or(HealthAnalysisError::Database)?;
            *value = json!(number);
        }
    }
    let updated_at =
        StoredInstant::try_from(updated_at).map_err(|_| HealthAnalysisError::Database)?;
    values.insert("updated_at".to_owned(), json!(updated_at.to_string()));
    Ok(row)
}
