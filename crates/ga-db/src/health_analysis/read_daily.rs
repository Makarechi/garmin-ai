use futures_util::TryStreamExt;
use ga_domain::{
    legacy_metrics::legacy_metric_contracts, model_consent::ModelCategories,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{HealthAnalysisError, HealthPeriod, begin_daily_analysis, begin_verified_analysis};
use crate::{Database, model_gate::onboarding::onboarding_allows_model};

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

pub struct DailyHealthReader(pub(super) Transaction<'static, Postgres>);

impl Database {
    pub async fn verified_health_reader(&self) -> Result<DailyHealthReader, HealthAnalysisError> {
        begin_verified_analysis(&self.work)
            .await
            .map(DailyHealthReader)
    }

    pub async fn read_health_range(
        &self,
        period: HealthPeriod,
    ) -> Result<Value, HealthAnalysisError> {
        DailyHealthReader(begin_daily_analysis(&self.work).await?)
            .range(period)
            .await
    }

    pub async fn read_health_snapshot(&self, day: Date) -> Result<Value, HealthAnalysisError> {
        DailyHealthReader(begin_daily_analysis(&self.work).await?)
            .snapshot(day)
            .await
    }
}

impl DailyHealthReader {
    /// Check the enrolled model audience in the same snapshot as its data.
    pub async fn permits_model_health(&mut self, owner: Uuid) -> Result<bool, HealthAnalysisError> {
        let current: bool =
            sqlx::query_scalar("SELECT EXISTS(SELECT FROM people WHERE singleton AND id=$1)")
                .bind(owner)
                .fetch_one(&mut *self.0)
                .await?;
        Ok(current && onboarding_allows_model(&mut self.0, ModelCategories::HEALTH).await?)
    }

    /// Check every required pack in the same snapshot as the outgoing model data.
    pub async fn permits_model_packs(
        &mut self,
        owner: Uuid,
        packs: &[&str],
    ) -> Result<bool, HealthAnalysisError> {
        model_packs_allowed(&mut self.0, owner, packs).await
    }

    /// Return recorded summaries for this inclusive calendar range.
    pub async fn range(mut self, period: HealthPeriod) -> Result<Value, HealthAnalysisError> {
        let result = range_in(&mut self.0, period).await?;
        self.0.commit().await?;
        Ok(result)
    }

    /// Show the stored calendar day, including missing metrics and the separate hydration contract.
    pub async fn snapshot(mut self, day: Date) -> Result<Value, HealthAnalysisError> {
        let result = snapshot_in(&mut self.0, day).await?;
        self.0.commit().await?;
        Ok(result)
    }
}

pub(super) async fn range_in(
    txn: &mut Transaction<'_, Postgres>,
    period: HealthPeriod,
) -> Result<Value, HealthAnalysisError> {
    let mut rows = Vec::new();
    {
        let mut stream = sqlx::query_as::<_, (String, OffsetDateTime)>(DAILY_ROWS)
            .bind(period.start)
            .bind(period.end)
            .fetch(&mut **txn);
        while let Some((document, updated_at)) = stream.try_next().await? {
            rows.push(health_day(&document, updated_at)?);
        }
    }
    let mut response = json!({
        "start": period.start.to_string(),
        "end": period.end.to_string(),
        "days_available": rows.len(),
        "days_requested": period.days(),
    });
    response["rows"] = Value::Array(rows);
    Ok(response)
}

pub(super) async fn snapshot_in(
    txn: &mut Transaction<'_, Postgres>,
    day: Date,
) -> Result<Value, HealthAnalysisError> {
    if !(1..=9999).contains(&day.year()) {
        return Err(HealthAnalysisError::DateRange);
    }
    let row = sqlx::query_as::<_, (String, OffsetDateTime)>(DAILY_ROWS)
        .bind(day)
        .bind(day)
        .fetch_optional(&mut **txn)
        .await?;
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

pub(super) async fn model_packs_allowed(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    packs: &[&str],
) -> Result<bool, HealthAnalysisError> {
    Ok(sqlx::query_scalar(
        "SELECT NOT EXISTS(SELECT FROM module_configs WHERE owner_id=$1)
                OR NOT EXISTS(
                    SELECT FROM unnest($2::text[]) AS required(key)
                    WHERE NOT EXISTS(
                        SELECT FROM module_configs
                        WHERE owner_id=$1 AND pack_key=required.key AND llm_enabled
                    )
                )",
    )
    .bind(owner)
    .bind(packs)
    .fetch_one(&mut **txn)
    .await?)
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
