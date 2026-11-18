use std::time::Duration;

use ga_domain::event_input::{known_timezone, local_midnight};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{FromRow, Row, postgres::PgRow};
use time::Date;

use super::{
    DailyHealthReader, HealthAnalysisError, HealthPeriod, SeriesReadError, owner_timezone,
};

struct ObservedHour {
    hour: i32,
    over_limit: bool,
    scheduled: f64,
    covered: f64,
    elevated: f64,
    weighted: f64,
    days: i64,
}

impl FromRow<'_, PgRow> for ObservedHour {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            hour: row.try_get("hour")?,
            over_limit: row.try_get("over_limit")?,
            scheduled: row.try_get("scheduled")?,
            covered: row.try_get("covered")?,
            elevated: row.try_get("elevated")?,
            weighted: row.try_get("weighted")?,
            days: row.try_get("days")?,
        })
    }
}

#[derive(Clone, Serialize)]
struct Hour {
    hour: i32,
    observed_days: i64,
    coverage_ratio: f64,
    observed_hours: f64,
    elevated_share: Option<f64>,
    mean_score: Option<f64>,
}

impl DailyHealthReader {
    /// Rank measured local hours only when their time and day coverage are sufficient.
    pub async fn stress_hours(
        mut self,
        start: Date,
        end: Date,
        requested_timezone: Option<&str>,
        configured_timezone: &str,
    ) -> Result<Value, SeriesReadError> {
        let period = HealthPeriod::new(start, end)?;
        if period.days() > 31 {
            return Err(SeriesReadError::Invalid);
        }
        let work = async {
            let timezone = match requested_timezone.filter(|value| !value.is_empty()) {
                Some(timezone) => timezone.to_owned(),
                None => owner_timezone(&mut self.0, configured_timezone).await?,
            };
            if !known_timezone(&timezone) {
                return Err(HealthAnalysisError::InvalidTimezone.into());
            }
            let left = local_midnight(start, &timezone)
                .map(time::OffsetDateTime::from)
                .ok_or(HealthAnalysisError::InvalidTimezone)?;
            let right = end
                .next_day()
                .and_then(|day| local_midnight(day, &timezone))
                .map(time::OffsetDateTime::from)
                .ok_or(HealthAnalysisError::InvalidTimezone)?;
            sqlx::query("SET LOCAL statement_timeout = '1500ms'")
                .execute(&mut *self.0)
                .await?;
            let observed: Vec<ObservedHour> = sqlx::query_as(include_str!("stress.sql"))
                .bind(left)
                .bind(right)
                .bind(&timezone)
                .fetch_all(&mut *self.0)
                .await?;
            let mut hours = Vec::with_capacity(24);
            for row in observed {
                if row.over_limit {
                    return Err(SeriesReadError::SampleLimit);
                }
                let ratio = if row.scheduled > 0.0 {
                    row.covered / row.scheduled
                } else {
                    0.0
                };
                let eligible = ratio >= 0.5 && row.days >= 3;
                hours.push(Hour {
                    hour: row.hour,
                    observed_days: row.days,
                    coverage_ratio: round(ratio, 4)?,
                    observed_hours: round(row.covered / 3600.0, 2)?,
                    elevated_share: eligible
                        .then(|| round(row.elevated / row.covered, 4))
                        .transpose()?,
                    mean_score: eligible
                        .then(|| round(row.weighted / row.covered, 1))
                        .transpose()?,
                });
            }
            self.0.commit().await?;
            let mut ranked: Vec<_> = hours
                .iter()
                .filter(|row| row.elevated_share.is_some())
                .cloned()
                .collect();
            ranked.sort_by(|a, b| {
                b.elevated_share
                    .unwrap_or_default()
                    .total_cmp(&a.elevated_share.unwrap_or_default())
                    .then_with(|| b.coverage_ratio.total_cmp(&a.coverage_ratio))
                    .then_with(|| a.hour.cmp(&b.hour))
            });
            Ok(json!({
                "metric":"stress_score","source":"garmin_connect","timezone":timezone,
                "start":start.to_string(),"end":end.to_string(),"threshold":51,
                "status":if ranked.is_empty(){"insufficient_coverage"}else{"ok"},
                "ranked_hours":ranked,"hours":hours,
                "method":"Share of observed time with Garmin stress score at or above threshold; gaps over five minutes are excluded",
                "limitations":[
                    "Garmin stress score is a physiological estimate, not a diary report of feeling stressed",
                    "Hours with under 50% coverage or fewer than three observed days are not ranked",
                    "Missing measurements are not treated as low stress",
                ],
            }))
        };
        tokio::time::timeout(Duration::from_millis(1800), work)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
}

fn round(value: f64, places: usize) -> Result<f64, HealthAnalysisError> {
    format!("{value:.places$}")
        .parse()
        .map_err(|_| HealthAnalysisError::Database)
}
