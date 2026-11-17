use ga_domain::{
    event_input::{local_midnight, source_calendar_date},
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{HealthAnalysisError, begin_verified_analysis};
use crate::Database;

type Intraday = (
    String,
    String,
    i32,
    i32,
    String,
    Option<OffsetDateTime>,
    Option<Uuid>,
    Option<OffsetDateTime>,
    Option<OffsetDateTime>,
    f64,
    f64,
    bool,
);

type Daily = (String, String, i32, Option<Date>, Option<String>);

impl Database {
    /// Read conservative current-state evidence after the recorded archive is ready.
    pub async fn observation_freshness(
        &self,
        at: StoredInstant,
        timezone: &str,
        endpoints: &Map<String, Value>,
    ) -> Result<Value, HealthAnalysisError> {
        let today =
            source_calendar_date(at, timezone).ok_or(HealthAnalysisError::InvalidTimezone)?;
        let midnight =
            local_midnight(today, timezone).ok_or(HealthAnalysisError::InvalidTimezone)?;
        let mut txn = begin_verified_analysis(&self.work).await?;
        let intraday = sqlx::query_as::<_, Intraday>(include_str!("freshness/intraday.sql"))
            .bind(OffsetDateTime::from(at))
            .bind(OffsetDateTime::from(midnight))
            .bind(serde_json::to_string(endpoints).map_err(|_| HealthAnalysisError::Database)?)
            .fetch_all(&mut *txn)
            .await?;
        let daily = sqlx::query_as::<_, Daily>(include_str!("freshness/daily.sql"))
            .bind(today)
            .fetch_all(&mut *txn)
            .await?;
        txn.commit().await?;

        let mut channels = Map::new();
        for row in intraday {
            let endpoint = endpoints.get(&row.1).unwrap_or(&Value::Null);
            let (metric, channel) = intraday_document(row, at, midnight, endpoint)?;
            channels.insert(metric, channel);
        }
        for (metric, endpoint, max_age, day, source_ref) in daily {
            let age = day.map(|day| (today - day).whole_days());
            let recent = age.is_some_and(|age| age <= i64::from(max_age));
            let quality = if recent {
                "recent_daily_summary"
            } else if day.is_some() {
                "unknown"
            } else {
                "not_synced"
            };
            channels.insert(
                metric,
                json!({
                    "endpoint": endpoint,
                    "semantics": "daily_summary",
                    "source_calendar_date": day.map(|day| day.to_string()),
                    "age_calendar_days": age,
                    "newest_observed_at": null,
                    "observation_lag_seconds": null,
                    "coverage_ratio": null,
                    "quality_reason": quality,
                    "usable_for_current_state": false,
                    "usable_as_daily_summary": recent,
                    "source_ref": source_ref,
                }),
            );
        }
        Ok(Value::Object(channels))
    }
}

fn intraday_document(
    row: Intraday,
    at: StoredInstant,
    midnight: StoredInstant,
    technical: &Value,
) -> Result<(String, Value), HealthAnalysisError> {
    let (
        metric,
        endpoint,
        gap,
        lag_limit,
        refresh,
        newest,
        source,
        first,
        last,
        covered,
        recent,
        returned,
    ) = row;
    let newest = timestamp(newest)?;
    let lag = newest.map(|newest| elapsed(at, newest));
    let recent_ratio = recent / f64::from(lag_limit);
    let time_today = elapsed(at, midnight);
    let returned_status = technical["status"].as_str();
    let assessed_status = if technical["source_ref"]
        .as_str()
        .is_some_and(|source| !source.is_empty())
        && !matches!(returned_status, Some("error" | "fetch_error"))
        && !returned
    {
        Some("empty")
    } else {
        returned_status
    };
    let quality = match assessed_status {
        Some("empty") => "source_empty",
        Some("error") => "parser_error",
        Some("fetch_error") => "fetch_error",
        _ if lag.is_none() && technical.as_object().is_none_or(Map::is_empty) => "not_synced",
        _ if lag.is_none() => "unknown",
        _ if lag.is_some_and(|lag| lag > f64::from(lag_limit)) => "stale_observation",
        _ if recent_ratio < 0.8 => "partial",
        _ => "recent_observations",
    };
    let start = timestamp(first)?.map(|time| time.to_string());
    let end = timestamp(last)?.map(|time| time.to_string());
    let coverage = (time_today > 0.0).then(|| covered / time_today);
    Ok((
        metric,
        json!({
            "endpoint": endpoint,
            "semantics": "intraday_samples",
            "refresh_mode": refresh,
            "newest_observed_at": newest.map(|time| time.to_string()),
            "observation_lag_seconds": lag,
            "expected_interval": {"start": midnight.to_string(), "end": at.to_string()},
            "observed_interval": {"start": start, "end": end},
            "coverage_ratio": coverage,
            "covered_seconds": covered,
            "recent_coverage_ratio": recent_ratio,
            "max_gap_seconds": gap,
            "max_lag_seconds": lag_limit,
            "quality_reason": quality,
            "usable_for_current_state": quality == "recent_observations",
            "source_updated_at": technical["source_updated_at"],
            "fetch_status": technical["status"],
            "fetch_lag_seconds": technical["fetch_lag_seconds"],
            "source_ref": source.map(|id| id.to_string()),
            "fetch_source_ref": technical["source_ref"],
        }),
    ))
}

fn timestamp(time: Option<OffsetDateTime>) -> Result<Option<StoredInstant>, HealthAnalysisError> {
    time.map(StoredInstant::try_from)
        .transpose()
        .map_err(|_| HealthAnalysisError::Database)
}

fn elapsed(at: StoredInstant, from: StoredInstant) -> f64 {
    (at.unix_microseconds() - from.unix_microseconds()) as f64 / 1_000_000.0
}
