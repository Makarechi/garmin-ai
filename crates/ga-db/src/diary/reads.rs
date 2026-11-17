use std::{error::Error, fmt, time::Duration};

use futures_util::TryStreamExt;
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, Month, OffsetDateTime};

use super::{EventSnapshotRow, event_snapshot};
use crate::{Database, ReadinessError, check_readiness, system_definitions::known_system_event};

#[derive(Clone, Copy, Debug)]
pub enum DiaryReadError {
    Storage(ReadinessError),
    TimestampRange,
    CalendarRange,
    CalendarBoundary,
    UnknownKind,
    Limit,
    Database,
    InvalidEvent,
}

impl fmt::Display for DiaryReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Diary storage is unavailable",
            Self::TimestampRange => "Timestamp range must be ordered and bounded",
            Self::CalendarRange => "Date range must be ordered and at most 3661 days",
            Self::CalendarBoundary => "Dates must allow bounded analysis-window expansion",
            Self::UnknownKind => "Unknown event kind",
            Self::Limit => "Invalid event limit",
            Self::Database => "Diary entries could not be read",
            Self::InvalidEvent => "Recorded diary entry could not be interpreted",
        })
    }
}

impl Error for DiaryReadError {}

impl From<sqlx::Error> for DiaryReadError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Page the owner's diary. This result is intended for the owner read path.
    pub async fn list_owner_events(
        &self,
        start: StoredInstant,
        end: StoredInstant,
        kind: Option<&str>,
        limit: u16,
    ) -> Result<Value, DiaryReadError> {
        let (start, end) = (OffsetDateTime::from(start), OffsetDateTime::from(end));
        check_range(start, end)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self
                .work
                .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                .await?;
            sqlx::query("SET LOCAL statement_timeout = '750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(DiaryReadError::Storage)?;
            let kind = stored_kind(&mut txn, kind).await?;
            if !(1..=1000).contains(&limit) {
                return Err(DiaryReadError::Limit);
            }
            let response = read_events(&mut txn, start, end, kind.as_deref(), limit).await?;
            txn.commit().await?;
            Ok(response)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }
}

fn check_range(start: OffsetDateTime, end: OffsetDateTime) -> Result<(), DiaryReadError> {
    let earliest = Date::from_calendar_date(1, Month::March, 2)
        .map_err(|_| DiaryReadError::CalendarBoundary)?;
    let latest = Date::from_calendar_date(9999, Month::November, 1)
        .map_err(|_| DiaryReadError::CalendarBoundary)?;
    if start.date() < earliest || end.date() > latest {
        return Err(DiaryReadError::CalendarBoundary);
    }
    if !(0..=3661).contains(&(end.date() - start.date()).whole_days()) {
        return Err(DiaryReadError::CalendarRange);
    }
    if start >= end || end - start > time::Duration::days(3660) {
        return Err(DiaryReadError::TimestampRange);
    }
    Ok(())
}

async fn stored_kind(
    txn: &mut Transaction<'_, Postgres>,
    kind: Option<&str>,
) -> Result<Option<String>, DiaryReadError> {
    let Some(kind) = kind else {
        return Ok(None);
    };
    if known_system_event(kind).map_err(|_| DiaryReadError::InvalidEvent)? {
        return Ok(Some(kind.to_owned()));
    }
    let definition: Option<(String, String)> = sqlx::query_as(
        "SELECT namespace,key FROM event_definitions WHERE key=$1 AND status IN ('active','retired')",
    )
    .bind(kind)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((namespace, key)) = definition else {
        return Err(DiaryReadError::UnknownKind);
    };
    let key = if namespace == "system" {
        key.strip_prefix("system.").unwrap_or(&key)
    } else {
        &key
    };
    Ok(Some(key.to_owned()))
}

async fn read_events(
    txn: &mut Transaction<'_, Postgres>,
    start: OffsetDateTime,
    end: OffsetDateTime,
    kind: Option<&str>,
    limit: u16,
) -> Result<Value, DiaryReadError> {
    type EventRow = (
        String,
        OffsetDateTime,
        Option<OffsetDateTime>,
        OffsetDateTime,
        OffsetDateTime,
        OffsetDateTime,
        OffsetDateTime,
        bool,
    );
    let mut rows = Vec::new();
    let mut query = sqlx::query_as::<_, EventRow>(include_str!("reads/events.sql"))
        .bind(start)
        .bind(end)
        .bind(kind)
        .bind(i64::from(limit) + 1)
        .fetch(&mut **txn);
    let mut truncated = false;
    while let Some((document, start, end, created, updated, recorded, ingested, can_update)) =
        query.try_next().await?
    {
        if rows.len() == usize::from(limit) {
            truncated = true;
            break;
        }
        let snapshot: EventSnapshotRow =
            (document, start, end, created, updated, recorded, ingested);
        rows.push(serialize(snapshot, can_update)?);
    }
    let mut response = json!({"truncated": truncated});
    response["rows"] = Value::Array(rows);
    Ok(response)
}

fn serialize(row: EventSnapshotRow, can_update: bool) -> Result<Value, DiaryReadError> {
    let mut event = event_snapshot(row).map_err(|_| DiaryReadError::InvalidEvent)?;
    let confidence = event["confidence"]
        .as_f64()
        .ok_or(DiaryReadError::InvalidEvent)?;
    event["confidence"] = json!(confidence);
    event["canonical"] = json!({
        "version": event["envelope_version"],
        "definition_version_id": event["definition_version_id"],
        "observed": {
            "start": event["start"], "end": event["end"], "timezone": event["timezone"],
            "topology": event["topology"], "precision": event["time_precision"],
        },
        "recorded_at": event["recorded_at"],
        "ingested_at": event["ingested_at"],
        "provenance": {
            "assertion_kind": event["assertion_kind"], "producer": event["producer"],
            "transport": event["transport"], "author": event["author"],
            "evidence_refs": event["evidence_refs"], "confidence": confidence,
            "validation_status": event["validation_status"],
        },
    });
    let open = event["topology"] == "open_interval" && event["end"].is_null();
    event["ongoing"] = json!(open);
    event["missing_end"] = json!(open);
    event["can_update"] = json!(can_update);
    if event["kind"] == "caffeine" {
        event["caffeine_total"] = caffeine_total(&event["payload"])?;
    }
    Ok(event)
}

fn caffeine_total(payload: &Value) -> Result<Value, DiaryReadError> {
    let unknown = json!("unknown");
    let basis = payload.get("dose_basis").unwrap_or(&unknown);
    let dose = |field: &str| -> Result<Option<f64>, DiaryReadError> {
        let Some(value) = payload.get(field).filter(|value| !value.is_null()) else {
            return Ok(None);
        };
        if !matches!(basis.as_str(), Some("total" | "per_serving")) {
            return Ok(None);
        }
        let value = value.as_f64().ok_or(DiaryReadError::InvalidEvent)?;
        let servings = if basis == "per_serving" {
            payload
                .get("servings")
                .map_or(Some(1.0), Value::as_f64)
                .ok_or(DiaryReadError::InvalidEvent)?
        } else {
            1.0
        };
        Ok(Some(value * servings))
    };
    let min = dose("caffeine_mg_min")?;
    let estimate = dose("caffeine_mg_estimate")?;
    let max = dose("caffeine_mg_max")?;
    Ok(json!({
        "min": min,
        "estimate": estimate,
        "max": max,
        "unit": "mg",
        "basis": "total",
        "source_basis": basis,
        "provenance": payload.get("dose_provenance").unwrap_or(&unknown),
        "status": if min.or(estimate).or(max).is_some() { "available" } else { "unknown" },
    }))
}
