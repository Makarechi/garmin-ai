use std::{error::Error, fmt, time::Duration};

use ga_domain::{
    legacy_json::{Utf8Layout, encode},
    timestamp::StoredInstant,
};
use serde::Serialize;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{DailyHealthReader, HealthAnalysisError, check_calendar_bounds};
use crate::{Database, replay::GARMIN_PARSER_VERSION};

const MAX_ACTIVITIES: u16 = 200;
const MAX_RECORDS: usize = 200;
const ROW_BUDGET: usize = 22_000;
const RESPONSE_BUDGET: usize = 24_000;
const LIMITATIONS: &[&str] = &[
    "Activity-scoped observations, not a complete device inventory or continuous settings history",
    "Device index is local to a FIT file; matching indices across activities do not establish identity",
    "Recorded device presence does not prove which sensor supplied heart-rate samples",
    "Zones are preserved as recorded; no physiological zone inference or historical recalculation",
    "Missing evidence means unavailable; stale evidence is from an older FIT projection",
];

#[derive(Debug)]
pub enum DeviceHistoryError {
    CalendarRange,
    TimestampRange,
    Limit,
    Analysis(HealthAnalysisError),
}

impl fmt::Display for DeviceHistoryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CalendarRange => f.write_str("Date range must be ordered and at most 367 days"),
            Self::TimestampRange => f.write_str("Timestamp range must be ordered and bounded"),
            Self::Limit => f.write_str("Use a positive range up to 366 days and limit 1..200"),
            Self::Analysis(error) => error.fmt(f),
        }
    }
}
impl Error for DeviceHistoryError {}

impl From<HealthAnalysisError> for DeviceHistoryError {
    fn from(error: HealthAnalysisError) -> Self {
        Self::Analysis(error)
    }
}
impl From<sqlx::Error> for DeviceHistoryError {
    fn from(_: sqlx::Error) -> Self {
        HealthAnalysisError::Database.into()
    }
}

type Activity = (
    String,
    OffsetDateTime,
    String,
    Option<String>,
    Option<Uuid>,
    Option<i32>,
    bool,
);

#[derive(Serialize)]
struct Record {
    kind: String,
    sequence: i32,
    fields: Value,
}

#[derive(Serialize)]
struct DeviceRow {
    activity_id: String,
    start: String,
    sport: String,
    evidence_status: &'static str,
    fit_archive_ref: Option<String>,
    source_ref: Option<String>,
    parser_version: Option<i32>,
    records: Vec<Record>,
    records_truncated: bool,
}

#[derive(Serialize)]
struct History {
    rows: Vec<DeviceRow>,
    truncated: bool,
    limitations: &'static [&'static str],
}

impl Database {
    /// Return bounded FIT device and zone evidence without reading private part fields.
    pub async fn device_history(
        &self,
        start: StoredInstant,
        end: StoredInstant,
        limit: u16,
    ) -> Result<Value, DeviceHistoryError> {
        let (start, end) = device_range(start, end, limit)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            self.verified_health_reader()
                .await?
                .read_device_history(start, end, limit)
                .await
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }
}

impl DailyHealthReader {
    pub async fn device_history(
        self,
        start: StoredInstant,
        end: StoredInstant,
        limit: u16,
    ) -> Result<Value, DeviceHistoryError> {
        let (start, end) = device_range(start, end, limit)?;
        tokio::time::timeout(
            Duration::from_millis(1500),
            self.read_device_history(start, end, limit),
        )
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }

    async fn read_device_history(
        mut self,
        start: OffsetDateTime,
        end: OffsetDateTime,
        limit: u16,
    ) -> Result<Value, DeviceHistoryError> {
        let result = read_history(&mut self.0, start, end, limit).await?;
        self.0.commit().await?;
        serde_json::to_value(result).map_err(|_| HealthAnalysisError::Database.into())
    }
}

fn device_range(
    start: StoredInstant,
    end: StoredInstant,
    limit: u16,
) -> Result<(OffsetDateTime, OffsetDateTime), DeviceHistoryError> {
    let (start, end) = (OffsetDateTime::from(start), OffsetDateTime::from(end));
    check_calendar_bounds(start.date(), end.date())?;
    if !(0..=367).contains(&(end.date() - start.date()).whole_days()) {
        return Err(DeviceHistoryError::CalendarRange);
    }
    if start >= end || end - start > time::Duration::days(366) {
        return Err(DeviceHistoryError::TimestampRange);
    }
    if !(1..=MAX_ACTIVITIES).contains(&limit) {
        return Err(DeviceHistoryError::Limit);
    }
    Ok((start, end))
}

async fn read_history(
    txn: &mut Transaction<'_, Postgres>,
    start: OffsetDateTime,
    end: OffsetDateTime,
    limit: u16,
) -> Result<History, DeviceHistoryError> {
    let activities = sqlx::query_as::<_, Activity>(
        "SELECT activity.id, activity.start, activity.kind, activity.parsed_key,
                raw.id, raw.parser_version,
                COALESCE(activity.fit_key != '' AND activity.fit_key=activity.parsed_key
                         AND raw.status='normalized' AND raw.parser_version=$4,false) AS current
         FROM (
             SELECT id,start,kind,fit_key,details->>'parsed_fit_key' AS parsed_key
             FROM activities WHERE start >= $1 AND start < $2
             ORDER BY start,id LIMIT $3
         ) AS activity
         LEFT JOIN LATERAL (
             SELECT id,parser_version,status FROM source_payloads
             WHERE source='garmin_connect' AND endpoint='activity_fit' AND source_key=activity.id
               AND archive_key=activity.parsed_key
             ORDER BY id LIMIT 1
         ) AS raw ON true
         ORDER BY activity.start,activity.id",
    )
    .bind(start)
    .bind(end)
    .bind(i64::from(limit) + 1)
    .bind(GARMIN_PARSER_VERSION)
    .fetch_all(&mut **txn)
    .await?;
    let limit = usize::from(limit);
    let mut result = History {
        rows: Vec::new(),
        truncated: activities.len() > limit,
        limitations: LIMITATIONS,
    };
    for (id, start, sport, fit_archive_ref, source_ref, parser_version, current) in
        activities.into_iter().take(limit)
    {
        let (records, records_truncated) = read_records(txn, &id).await?;
        let evidence_status = match (records.is_empty(), current) {
            (true, _) => "unavailable",
            (false, true) => "available",
            (false, false) => "stale",
        };
        let start = StoredInstant::try_from(start).map_err(|_| HealthAnalysisError::Database)?;
        result.rows.push(DeviceRow {
            activity_id: id,
            start: start.to_string(),
            sport,
            evidence_status,
            fit_archive_ref,
            source_ref: source_ref.map(|reference| reference.to_string()),
            parser_version,
            records,
            records_truncated,
        });
        if json_bytes(&result.rows)? > ROW_BUDGET {
            result.truncated = true;
            if result.rows.len() > 1 {
                result.rows.pop();
            } else {
                while json_bytes(&result.rows)? > ROW_BUDGET && trim_records(&mut result.rows) {}
            }
            break;
        }
    }
    while json_bytes(&result)? > RESPONSE_BUDGET {
        result.truncated = true;
        if result.rows.len() > 1 {
            result.rows.pop();
        } else if !trim_records(&mut result.rows) {
            result.rows.clear();
            break;
        }
    }
    Ok(result)
}

async fn read_records(
    txn: &mut Transaction<'_, Postgres>,
    id: &str,
) -> Result<(Vec<Record>, bool), DeviceHistoryError> {
    let parts = sqlx::query_as::<_, (String, i32, Option<String>)>(include_str!(
        "device_history/records.sql"
    ))
    .bind(id)
    .bind(MAX_RECORDS as i64)
    .fetch_all(&mut **txn)
    .await?;
    let mut records = Vec::new();
    let mut truncated = false;
    for (kind, sequence, fields) in parts {
        if let Some(fields) = fields {
            records.push(Record {
                kind,
                sequence,
                fields: serde_json::from_str(&fields).map_err(|_| HealthAnalysisError::Database)?,
            });
        } else {
            truncated = true;
        }
    }
    Ok((records, truncated))
}

fn trim_records(rows: &mut [DeviceRow]) -> bool {
    if let Some(row) = rows.first_mut()
        && row.records.pop().is_some()
    {
        row.records_truncated = true;
        return true;
    }
    false
}

fn json_bytes(value: &impl Serialize) -> Result<usize, DeviceHistoryError> {
    let bytes = serde_json::to_vec(value).map_err(|_| HealthAnalysisError::Database)?;
    let legacy = encode(&bytes, Utf8Layout::Archive, bytes.len())
        .map_err(|_| HealthAnalysisError::Database)?;
    Ok(legacy.len())
}
