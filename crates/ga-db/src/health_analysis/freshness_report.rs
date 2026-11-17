use std::time::Duration;

use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value, json};
use sqlx::{FromRow, Postgres, Row, Transaction, postgres::PgRow};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{
    HealthAnalysisError, begin_daily_analysis,
    freshness::{freshness_day, observation_freshness_in, timestamp},
};
use crate::{
    Database,
    history_windows::history_status_in,
    replay::{GARMIN_PARSER_VERSION, replay_status_in},
};

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
        let (today, midnight) = freshness_day(at, timezone)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = begin_daily_analysis(&self.work).await?;
            let report = read_report(&mut txn, at, today, midnight).await?;
            txn.commit().await?;
            Ok(report)
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }
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
