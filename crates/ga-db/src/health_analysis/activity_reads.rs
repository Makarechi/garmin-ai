mod nonsample;

use std::{error::Error, fmt, time::Duration};

use futures_util::TryStreamExt;
use ga_domain::{
    legacy_json::{Utf8Layout, encode},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use self::nonsample::NON_SAMPLE_FIT;
use super::{DailyHealthReader, HealthAnalysisError, MAX_RANGE_DAYS, check_calendar_bounds};
use crate::Database;

#[derive(Clone, Copy, Debug)]
pub enum ActivityReadError {
    Range,
    CalendarRange,
    Limit,
    HeartRateRange,
    ActivityAnalysisLimit,
    Pagination,
    Missing,
    ResultTooLarge,
    Analysis(HealthAnalysisError),
}

impl fmt::Display for ActivityReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Range => f.write_str("Timestamp range must be ordered and bounded"),
            Self::CalendarRange => f.write_str("Date range must be ordered and at most 3661 days"),
            Self::Limit => f.write_str("Invalid activity limit"),
            Self::HeartRateRange => f.write_str("Invalid heart-rate range"),
            Self::ActivityAnalysisLimit => {
                f.write_str("Limit analysis to 2000 activities; narrow the date range")
            }
            Self::Pagination => f.write_str("Invalid activity pagination"),
            Self::Missing => f.write_str("Activity not found"),
            Self::ResultTooLarge => f.write_str("Activity evidence exceeds the model result limit"),
            Self::Analysis(error) => error.fmt(f),
        }
    }
}

impl Error for ActivityReadError {}

impl From<HealthAnalysisError> for ActivityReadError {
    fn from(error: HealthAnalysisError) -> Self {
        Self::Analysis(error)
    }
}

impl From<sqlx::Error> for ActivityReadError {
    fn from(_: sqlx::Error) -> Self {
        HealthAnalysisError::Database.into()
    }
}

type ActivityRow = (String, OffsetDateTime, OffsetDateTime, OffsetDateTime);

const MODEL_RESULT_CHARS: usize = 24_000;
const ACTIVITY_FLOATS: &[&str] = &[
    "duration_seconds",
    "moving_seconds",
    "distance_m",
    "avg_hr",
    "max_hr",
    "avg_speed_mps",
    "calories",
    "cadence",
    "ascent_m",
    "descent_m",
    "aerobic_effect",
    "anaerobic_effect",
    "training_load",
];

impl Database {
    /// Return activity summaries whose start lies in the half-open window.
    pub async fn list_activities(
        &self,
        start: StoredInstant,
        end: StoredInstant,
        kind: Option<&str>,
        limit: u16,
    ) -> Result<Value, ActivityReadError> {
        let (start, end) = activity_range(start, end, limit)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            self.verified_health_reader()
                .await?
                .activity_summaries_in(start, end, kind, limit)
                .await
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }

    /// Fetch one activity and one page of details. Samples are explicitly opt-in.
    pub async fn read_activity_details(
        &self,
        activity_id: &str,
        include_samples: bool,
        offset: u32,
        limit: u16,
    ) -> Result<Value, ActivityReadError> {
        check_pagination(offset, limit)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            self.verified_health_reader()
                .await?
                .activity_details_in(activity_id, include_samples, offset, limit)
                .await
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }
}

impl DailyHealthReader {
    pub async fn activity_summaries(
        self,
        start: StoredInstant,
        end: StoredInstant,
        kind: Option<&str>,
        limit: u16,
    ) -> Result<Value, ActivityReadError> {
        let (start, end) = activity_range(start, end, limit)?;
        tokio::time::timeout(
            Duration::from_millis(1500),
            self.activity_summaries_in(start, end, kind, limit),
        )
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }

    async fn activity_summaries_in(
        mut self,
        start: OffsetDateTime,
        end: OffsetDateTime,
        kind: Option<&str>,
        limit: u16,
    ) -> Result<Value, ActivityReadError> {
        let result = summaries_in(&mut self.0, start, end, kind, limit, false).await?;
        self.0.commit().await?;
        Ok(result)
    }

    pub async fn activity_details(
        self,
        activity_id: &str,
        include_samples: bool,
        offset: u32,
        limit: u16,
    ) -> Result<Value, ActivityReadError> {
        check_pagination(offset, limit)?;
        tokio::time::timeout(
            Duration::from_millis(1500),
            self.activity_details_in(activity_id, include_samples, offset, limit),
        )
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }

    async fn activity_details_in(
        mut self,
        activity_id: &str,
        include_samples: bool,
        offset: u32,
        limit: u16,
    ) -> Result<Value, ActivityReadError> {
        let result = details_in(
            &mut self.0,
            activity_id,
            include_samples,
            offset,
            limit,
            false,
        )
        .await?;
        self.0.commit().await?;
        Ok(result)
    }
}

pub(super) async fn model_activity_summaries_in(
    txn: &mut Transaction<'_, Postgres>,
    start: StoredInstant,
    end: StoredInstant,
    kind: Option<&str>,
) -> Result<Value, ActivityReadError> {
    const LIMIT: u16 = 200;
    let (start, end) = activity_range(start, end, LIMIT)?;
    summaries_in(txn, start, end, kind, LIMIT, true).await
}

pub(super) async fn model_activity_details_in(
    txn: &mut Transaction<'_, Postgres>,
    activity_id: &str,
    include_samples: bool,
    offset: u32,
    limit: u16,
) -> Result<Value, ActivityReadError> {
    check_pagination(offset, limit)?;
    details_in(txn, activity_id, include_samples, offset, limit, true).await
}

async fn summaries_in(
    txn: &mut Transaction<'_, Postgres>,
    start: OffsetDateTime,
    end: OffsetDateTime,
    kind: Option<&str>,
    limit: u16,
    model: bool,
) -> Result<Value, ActivityReadError> {
    let mut remaining = model.then_some(MODEL_RESULT_CHARS);
    let mut rows = Vec::new();
    let mut results = sqlx::query_as::<_, ActivityRow>(
        r#"SELECT CASE WHEN octet_length(document) > $5 THEN '' ELSE document END,
                   start, "end", updated_at
            FROM (
                SELECT row_to_json(activity)::text AS document, start, "end", updated_at
                FROM activities AS activity WHERE start >= $1 AND start < $2
                    AND ($3::text IS NULL OR kind = $3)
                ORDER BY start LIMIT $4
            ) AS selected ORDER BY start"#,
    )
    .bind(start)
    .bind(end)
    .bind(kind.filter(|kind| !kind.is_empty()))
    .bind(i64::from(limit) + 1)
    .bind(document_byte_limit(model))
    .fetch(&mut **txn);
    let mut truncated = false;
    while let Some(row) = results.try_next().await? {
        if rows.len() == usize::from(limit) {
            truncated = true;
            break;
        }
        let row = activity(row)?;
        account_for_value(&row, &mut remaining)?;
        rows.push(row);
    }
    let response = page("rows", rows, truncated);
    account_for_value(&response, &mut model.then_some(MODEL_RESULT_CHARS))?;
    Ok(response)
}

async fn details_in(
    txn: &mut Transaction<'_, Postgres>,
    activity_id: &str,
    include_samples: bool,
    offset: u32,
    limit: u16,
    model: bool,
) -> Result<Value, ActivityReadError> {
    let mut remaining = model.then_some(MODEL_RESULT_CHARS);
    let row = sqlx::query_as::<_, ActivityRow>(
        r#"SELECT CASE WHEN octet_length(document) > $2 THEN '' ELSE document END,
                   start, "end", updated_at
            FROM (
                SELECT row_to_json(activity)::text AS document, start, "end", updated_at
                FROM activities AS activity WHERE id=$1
            ) AS selected"#,
    )
    .bind(activity_id)
    .bind(document_byte_limit(model))
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(ActivityReadError::Missing)?;
    let summary = activity(row)?;
    account_for_value(&summary, &mut remaining)?;
    let mut parts = Vec::new();
    let mut results =
        sqlx::query_scalar::<_, Option<String>>(include_str!("activity_reads/parts.sql"))
            .bind(activity_id)
            .bind(include_samples)
            .bind(i64::from(offset))
            .bind(i64::from(limit))
            .bind(NON_SAMPLE_FIT)
            .bind(document_byte_limit(model))
            .fetch(&mut **txn);
    let mut truncated = false;
    while let Some(document) = results.try_next().await? {
        match document {
            Some(document) if document.is_empty() => return Err(ActivityReadError::ResultTooLarge),
            Some(document) => {
                let part =
                    serde_json::from_str(&document).map_err(|_| HealthAnalysisError::Database)?;
                account_for_value(&part, &mut remaining)?;
                parts.push(part);
            }
            None => truncated = true,
        }
    }
    let mut response = page("parts", parts, truncated);
    response["activity"] = summary;
    response["next_offset"] = json!(truncated.then(|| offset + u32::from(limit)));
    account_for_value(&response, &mut model.then_some(MODEL_RESULT_CHARS))?;
    Ok(response)
}

fn document_byte_limit(model: bool) -> Option<i32> {
    model.then_some(i32::try_from(4 * MODEL_RESULT_CHARS).ok()?)
}

fn account_for_value(
    value: &Value,
    remaining: &mut Option<usize>,
) -> Result<(), ActivityReadError> {
    if let Some(remaining) = remaining {
        let raw = serde_json::to_vec(value).map_err(|_| HealthAnalysisError::Database)?;
        if raw.len() > *remaining * 4 {
            return Err(ActivityReadError::ResultTooLarge);
        }
        let encoded = encode(&raw, Utf8Layout::Ordered, raw.len())
            .map_err(|_| HealthAnalysisError::Database)?;
        let characters = std::str::from_utf8(&encoded)
            .map_err(|_| HealthAnalysisError::Database)?
            .chars()
            .count();
        *remaining = remaining
            .checked_sub(characters)
            .ok_or(ActivityReadError::ResultTooLarge)?;
    }
    Ok(())
}

fn activity_range(
    start: StoredInstant,
    end: StoredInstant,
    limit: u16,
) -> Result<(OffsetDateTime, OffsetDateTime), ActivityReadError> {
    let (start, end) = (OffsetDateTime::from(start), OffsetDateTime::from(end));
    check_calendar_bounds(start.date(), end.date())?;
    if !(0..=MAX_RANGE_DAYS + 1).contains(&(end.date() - start.date()).whole_days()) {
        return Err(ActivityReadError::CalendarRange);
    }
    if start >= end || end - start > time::Duration::days(MAX_RANGE_DAYS) {
        return Err(ActivityReadError::Range);
    }
    if !(1..=1000).contains(&limit) {
        return Err(ActivityReadError::Limit);
    }
    Ok((start, end))
}

fn check_pagination(offset: u32, limit: u16) -> Result<(), ActivityReadError> {
    if offset > 1_000_000 || !(1..=2000).contains(&limit) {
        return Err(ActivityReadError::Pagination);
    }
    Ok(())
}

fn page(key: &str, values: Vec<Value>, truncated: bool) -> Value {
    let mut response = json!({"truncated": truncated});
    response[key] = Value::Array(values);
    response
}

fn activity((document, start, end, updated_at): ActivityRow) -> Result<Value, ActivityReadError> {
    if document.is_empty() {
        return Err(ActivityReadError::ResultTooLarge);
    }
    let mut row: Value =
        serde_json::from_str(&document).map_err(|_| HealthAnalysisError::Database)?;
    let values = row.as_object_mut().ok_or(HealthAnalysisError::Database)?;
    values.retain(|key, _| {
        matches!(
            key.as_str(),
            "id" | "kind" | "name" | "timezone" | "fit_key" | "details"
        ) || ACTIVITY_FLOATS.contains(&key.as_str())
    });
    for field in ACTIVITY_FLOATS {
        let value = values
            .get_mut(*field)
            .ok_or(HealthAnalysisError::Database)?;
        if !value.is_null() {
            let number = value
                .as_f64()
                .filter(|value| value.is_finite())
                .ok_or(HealthAnalysisError::Database)?;
            *value = json!(number);
        }
    }
    for (key, time) in [("start", start), ("end", end), ("updated_at", updated_at)] {
        let time = StoredInstant::try_from(time).map_err(|_| HealthAnalysisError::Database)?;
        values.insert(key.to_owned(), json!(time.to_string()));
    }
    Ok(row)
}
