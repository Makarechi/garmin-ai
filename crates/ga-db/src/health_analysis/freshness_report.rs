use std::time::Duration;

use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value, json};
use sqlx::{FromRow, Postgres, Row, Transaction, postgres::PgRow};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{
    DailyHealthReader, HealthAnalysisError, begin_daily_analysis,
    freshness::{freshness_day, observation_freshness_in, timestamp},
    read_daily::model_packs_allowed,
};
use crate::{
    Database,
    history_windows::history_status_in,
    replay::{GARMIN_PARSER_VERSION, replay_status_in},
};

pub const MODEL_HEALTH_PACKS: &[&str] = &["general_diary", "sleep", "training", "wellbeing"];

pub fn model_metric_packs(metric: &str) -> &'static [&'static str] {
    let metric = metric.strip_prefix("system.").unwrap_or(metric);
    match metric {
        "hydration_ml" => &["general_diary"],
        "steps"
        | "steps_bucket"
        | "active_calories"
        | "intensity_minutes"
        | "training_status"
        | "training_readiness_score"
        | "recovery_time_minutes" => &["training"],
        "heart_rate_bpm" | "spo2_pct" | "respiration_rpm" | "resting_hr" => &["wellbeing"],
        _ if ["sleep_", "deep_", "rem_", "light_", "awake_"]
            .iter()
            .any(|prefix| metric.starts_with(prefix)) =>
        {
            &["sleep"]
        }
        _ if ["hrv_", "stress_", "body_battery_"]
            .iter()
            .any(|prefix| metric.starts_with(prefix)) =>
        {
            &["wellbeing"]
        }
        _ => MODEL_HEALTH_PACKS,
    }
}

struct Endpoint {
    endpoint: String,
    historical: bool,
    value: String,
    id: Option<Uuid>,
    source_updated_at: Option<OffsetDateTime>,
    payload_hash: Option<String>,
    parser_version: Option<i32>,
    status: Option<String>,
    fetch_lag: f64,
    success_lag: Option<f64>,
}

impl FromRow<'_, PgRow> for Endpoint {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            endpoint: row.try_get("endpoint")?,
            historical: row.try_get("historical")?,
            value: row.try_get("value")?,
            id: row.try_get("id")?,
            source_updated_at: row.try_get("source_updated_at")?,
            payload_hash: row.try_get("payload_hash")?,
            parser_version: row.try_get("parser_version")?,
            status: row.try_get("status")?,
            fetch_lag: row.try_get("fetch_lag")?,
            success_lag: row.try_get("success_lag")?,
        })
    }
}

impl Database {
    /// Report technical freshness and usable observations from one committed snapshot.
    pub async fn data_freshness(
        &self,
        at: StoredInstant,
        timezone: &str,
    ) -> Result<Value, HealthAnalysisError> {
        self.freshness_report(at, timezone, false).await
    }

    /// Prefer the saved owner timezone once onboarding has selected preferences.
    pub async fn owner_data_freshness(
        &self,
        at: StoredInstant,
        configured_timezone: &str,
    ) -> Result<Value, HealthAnalysisError> {
        self.freshness_report(at, configured_timezone, true).await
    }

    /// Return only model-visible observation channels, including when replay is pending.
    pub async fn model_data_freshness(
        &self,
        owner: Uuid,
        at: StoredInstant,
        configured_timezone: &str,
    ) -> Result<Option<Value>, HealthAnalysisError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut reader = DailyHealthReader(begin_daily_analysis(&self.work).await?);
            if !reader.permits_model_health(owner).await? {
                return Ok(None);
            }
            let timezone = super::owner_timezone(&mut reader.0, configured_timezone).await?;
            let (report, _) = model_freshness_in(&mut reader.0, owner, at, &timezone).await?;
            reader.0.commit().await?;
            Ok(Some(report))
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }

    async fn freshness_report(
        &self,
        at: StoredInstant,
        configured_timezone: &str,
        owner_preferences: bool,
    ) -> Result<Value, HealthAnalysisError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = begin_daily_analysis(&self.work).await?;
            let timezone = if owner_preferences {
                super::owner_timezone(&mut txn, configured_timezone).await?
            } else {
                configured_timezone.to_owned()
            };
            let report = data_freshness_in(&mut txn, at, &timezone).await?;
            txn.commit().await?;
            Ok(report)
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }
}

pub(super) async fn model_freshness_in(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    at: StoredInstant,
    timezone: &str,
) -> Result<(Value, bool), HealthAnalysisError> {
    let mut report = data_freshness_in(txn, at, timezone).await?;
    let ready = report["archive_replay"]["ready"]
        .as_bool()
        .ok_or(HealthAnalysisError::Database)?;
    let Value::Object(channels) = report["channels"].take() else {
        return Err(HealthAnalysisError::Database);
    };
    let mut visible = Map::new();
    for (metric, channel) in channels {
        if model_packs_allowed(txn, owner, model_metric_packs(&metric)).await? {
            visible.insert(metric, channel);
        }
    }
    Ok((
        json!({
            "checked_at": report["checked_at"],
            "available": !visible.is_empty(),
            "channels": visible,
            "limitations": report["limitations"],
        }),
        ready,
    ))
}

pub(crate) async fn data_freshness_in(
    txn: &mut Transaction<'_, Postgres>,
    at: StoredInstant,
    timezone: &str,
) -> Result<Value, HealthAnalysisError> {
    let (today, midnight) = freshness_day(at, timezone)?;
    read_report(txn, at, today, midnight).await
}

async fn read_report(
    txn: &mut Transaction<'_, Postgres>,
    at: StoredInstant,
    today: Date,
    midnight: StoredInstant,
) -> Result<Value, HealthAnalysisError> {
    let mut endpoints = Map::new();
    let mut historical = Map::new();
    for row in sqlx::query_as::<_, Endpoint>(include_str!("freshness_report/endpoints.sql"))
        .bind(today.to_string())
        .bind(OffsetDateTime::from(at))
        .fetch_all(&mut **txn)
        .await?
    {
        let mut value: Map<String, Value> =
            serde_json::from_str(&row.value).map_err(|_| HealthAnalysisError::Database)?;
        let success = value.get("success_at").cloned().unwrap_or(Value::Null);
        let fetched = value
            .get("fetched_at")
            .and_then(Value::as_str)
            .filter(|fetched| !fetched.is_empty())
            .or_else(|| success.as_str().filter(|success| !success.is_empty()));
        let Some(fetched) = fetched else {
            continue;
        };
        value.insert("fetched_at".into(), json!(fetched));
        value.insert("last_success_at".into(), success);
        value.insert("lag_seconds".into(), json!(row.success_lag));
        value.insert("fetch_lag_seconds".into(), json!(row.fetch_lag));
        if let Some(id) = row.id {
            value.insert("source_ref".into(), json!(id.to_string()));
            value.insert(
                "source_updated_at".into(),
                json!(timestamp(row.source_updated_at)?.map(|at| at.to_string())),
            );
            value.insert("source_revision".into(), json!(row.payload_hash));
            value.insert("parser_version".into(), json!(row.parser_version));
            value.insert("parser_status".into(), json!(row.status));
        }
        let target = if row.historical {
            &mut historical
        } else {
            &mut endpoints
        };
        target.insert(row.endpoint, Value::Object(value));
    }

    let connection: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='integration:garmin'")
            .fetch_optional(&mut **txn)
            .await?;
    let connection = match connection {
        Some(saved) => serde_json::from_str(&saved).map_err(|_| HealthAnalysisError::Database)?,
        None => json!({"status": "not_attempted"}),
    };
    let history = history_status_in(txn)
        .await
        .map_err(|_| HealthAnalysisError::Database)?;
    let replay = replay_status_in(txn, GARMIN_PARSER_VERSION)
        .await
        .map_err(|_| HealthAnalysisError::Database)?;
    let channels = if replay.ready {
        observation_freshness_in(txn, at, today, midnight, &endpoints).await?
    } else {
        json!({})
    };
    Ok(json!({
        "checked_at": at.to_string(),
        "available": !endpoints.is_empty() && replay.ready,
        "endpoints": endpoints,
        "historical": historical,
        "history_sync": history,
        "archive_replay": replay,
        "connection": connection,
        "channels": channels,
        "limitations": [
            "Fetch success does not establish fresh observations or complete device wear",
            "Coverage joins adjacent valid samples only; gaps are never filled",
            "Daily summaries have calendar semantics, not an invented measurement timestamp",
        ],
    }))
}
