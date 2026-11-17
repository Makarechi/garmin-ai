use std::time::Duration;

use ga_domain::{
    garmin_summary::gmt_instant,
    legacy_json::{self, Utf8Layout},
    sha256_hex,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{HealthAnalysisError, begin_daily_analysis};
use crate::{Database, activity_documents::PARSER_VERSION};

impl Database {
    /// Read one verified main interval for the wake date. This does not wait for
    /// unrelated replay activity or relax the storage and owner-lifecycle fence.
    pub async fn main_sleep_timing(
        &self,
        day: Date,
        timezone: &str,
    ) -> Result<Value, HealthAnalysisError> {
        if !(1..=9999).contains(&day.year()) {
            return Err(HealthAnalysisError::DateRange);
        }
        let read = async {
            let mut txn = begin_daily_analysis(&self.control).await?;
            let document = main_sleep_timing_in(&mut txn, day, timezone).await?;
            txn.rollback().await?;
            Ok(document)
        };
        tokio::time::timeout(Duration::from_millis(1500), read)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
}

async fn main_sleep_timing_in(
    txn: &mut Transaction<'_, Postgres>,
    day: Date,
    timezone: &str,
) -> Result<Value, HealthAnalysisError> {
    let day = day.to_string();
    let key = format!("sleep:{day}");
    let candidate: Option<Option<String>> = sqlx::query_scalar(
        "SELECT CASE WHEN jsonb_typeof(evidence->'source_ref')='string'
                        THEN evidence->>'source_ref' END FROM timeline_intervals WHERE id=$1",
    )
    .bind(&key)
    .fetch_optional(&mut **txn)
    .await?;
    let mut result = json!({
        "algorithm_version": "main-sleep-timing-v1",
        "timezone": timezone,
        "status": "unavailable",
        "rows": [{"day": day, "main_interval": null}],
        "limitations": ["Garmin's recorded main sleep interval, not proven sleep onset."],
    });
    let Some(reference) = candidate else {
        return Ok(result);
    };
    result["status"] = json!("unverified_source");
    let Some(reference) = reference else {
        return Ok(result);
    };
    let Ok(id) = Uuid::parse_str(&reference) else {
        return Ok(result);
    };
    let proof = sqlx::query_as::<_, (OffsetDateTime, OffsetDateTime, String)>(include_str!(
        "sleep_timing.sql"
    ))
    .bind(key)
    .bind(&day)
    .bind(id)
    .bind(&reference)
    .bind(PARSER_VERSION)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((known_start, known_end, encoded)) = proof else {
        return Ok(result);
    };
    let [calendar, start, end]: [Value; 3] =
        serde_json::from_str(&encoded).map_err(|_| HealthAnalysisError::Database)?;
    if !calendar.is_null() && calendar.as_str() != Some(&day) {
        return Ok(result);
    }
    let (Some(start), Some(end)) = (gmt_instant(&start), gmt_instant(&end)) else {
        return Ok(result);
    };
    if start >= end
        || StoredInstant::try_from(known_start).ok() != Some(start)
        || StoredInstant::try_from(known_end).ok() != Some(end)
    {
        return Ok(result);
    }
    result["status"] = json!("available");
    result["rows"][0]["main_interval"] = json!({
        "start": start.to_string(), "end": end.to_string(), "source_ref": id.to_string(),
    });
    Ok(result)
}

/// Hash a timing-only result using the stored Python-compatible representation.
/// A delivery check must recompute the result under its authorization fence.
pub fn sleep_timing_result_hash(result: &Value) -> Result<String, HealthAnalysisError> {
    let body = serde_json::to_vec(result).map_err(|_| HealthAnalysisError::Database)?;
    let encoded = legacy_json::encode_ascii(&body, Utf8Layout::Archive, 4096)
        .map_err(|_| HealthAnalysisError::Database)?;
    Ok(sha256_hex(encoded.as_bytes()))
}
