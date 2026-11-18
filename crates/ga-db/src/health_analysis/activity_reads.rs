mod nonsample;

use std::{error::Error, fmt, time::Duration};

use futures_util::TryStreamExt;
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
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
        let mut rows = Vec::new();
        let truncated = {
            let mut results = sqlx::query_as::<_, ActivityRow>(
                r#"SELECT row_to_json(activity)::text, start, "end", updated_at
                   FROM activities AS activity WHERE start >= $1 AND start < $2
                       AND ($3::text IS NULL OR kind = $3)
                   ORDER BY start LIMIT $4"#,
            )
            .bind(start)
            .bind(end)
            .bind(kind.filter(|kind| !kind.is_empty()))
            .bind(i64::from(limit) + 1)
            .fetch(&mut *self.0);
            let mut truncated = false;
            while let Some(row) = results.try_next().await? {
                if rows.len() == usize::from(limit) {
                    truncated = true;
                    break;
                }
                rows.push(activity(row)?);
            }
            truncated
        };
        self.0.commit().await?;
        Ok(page("rows", rows, truncated))
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
        let row = sqlx::query_as::<_, ActivityRow>(
            r#"SELECT row_to_json(activity)::text, start, "end", updated_at
                   FROM activities AS activity WHERE id=$1"#,
        )
        .bind(activity_id)
        .fetch_optional(&mut *self.0)
        .await?
        .ok_or(ActivityReadError::Missing)?;
        let summary = activity(row)?;
        let mut parts = Vec::new();
        let truncated = {
            let mut results =
                sqlx::query_scalar::<_, Option<String>>(include_str!("activity_reads/parts.sql"))
                    .bind(activity_id)
                    .bind(include_samples)
                    .bind(i64::from(offset))
                    .bind(i64::from(limit))
                    .bind(NON_SAMPLE_FIT)
                    .fetch(&mut *self.0);
            let mut truncated = false;
            while let Some(document) = results.try_next().await? {
                match document {
                    Some(document) => parts.push(
                        serde_json::from_str(&document)
                            .map_err(|_| HealthAnalysisError::Database)?,
                    ),
                    None => truncated = true,
                }
            }
            truncated
        };
        self.0.commit().await?;
        let mut response = page("parts", parts, truncated);
        response["activity"] = summary;
        response["next_offset"] = json!(truncated.then(|| offset + u32::from(limit)));
        Ok(response)
    }
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

fn activity((document, start, end, updated_at): ActivityRow) -> Result<Value, HealthAnalysisError> {
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
