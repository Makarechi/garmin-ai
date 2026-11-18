mod activity_reads;
mod device_history;
mod lagged;
mod read_daily;
mod running;
mod series;
pub use series::SeriesReadError;
mod sleep_analysis;
mod sleep_timing;

pub use activity_reads::ActivityReadError;
pub use device_history::DeviceHistoryError;
pub use read_daily::DailyHealthReader;
pub use sleep_analysis::NapPolicy;
pub use sleep_timing::sleep_timing_result_hash;

mod freshness;
mod temporal;
mod timeline;

pub(crate) use temporal::PRE_EVENT_FEATURE_VERSION;
pub use temporal::{TemporalFeatures, TemporalPurpose};
pub use timeline::TimelineError;

use std::{error::Error, fmt};

use ga_domain::statistics::{block_mean_difference, describe, standardized_difference};
use serde_json::{Value, json};
use sqlx::{PgPool, Postgres, Transaction};
use time::{Date, Month, OffsetDateTime};

use crate::{
    Database, ReadinessError,
    activity_documents::PARSER_VERSION,
    check_readiness,
    replay::{REPLAY_MUTATION_LOCK, projection_pending},
};

mod freshness_report;
pub(crate) use freshness_report::data_freshness_in;
pub use freshness_report::{MODEL_HEALTH_PACKS, model_metric_packs};

const MAX_RANGE_DAYS: i64 = 3660;

fn isoformat(at: OffsetDateTime) -> String {
    let fraction = if at.microsecond() == 0 {
        String::new()
    } else {
        format!(".{:06}", at.microsecond())
    };
    let seconds = at.offset().whole_seconds();
    let absolute = seconds.unsigned_abs();
    let sign = if seconds < 0 { '-' } else { '+' };
    let mut offset = format!("{sign}{:02}:{:02}", absolute / 3600, absolute / 60 % 60);
    if !absolute.is_multiple_of(60) {
        offset.push_str(&format!(":{:02}", absolute % 60));
    }
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}{fraction}{offset}",
        at.year(),
        u8::from(at.month()),
        at.day(),
        at.hour(),
        at.minute(),
        at.second()
    )
}

#[derive(Clone, Copy, Debug)]
pub enum HealthAnalysisError {
    UnknownMetric,
    DateRange,
    CalendarBoundary,
    Overlap,
    Storage(ReadinessError),
    ReplayPending,
    Database,
    InvalidTimezone,
    InvalidKnowledgeCutoff,
    TemporalVersionLimit,
    SleepRange,
    SleepNapLimit,
    SleepSessionLimit,
}

impl fmt::Display for HealthAnalysisError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::UnknownMetric => "Unknown daily metric",
            Self::DateRange => "Date range must be ordered and at most 3660 days",
            Self::CalendarBoundary => "Dates must allow bounded analysis-window expansion",
            Self::Overlap => "Comparison periods must not overlap",
            Self::Storage(_) => "Health analysis storage is unavailable",
            Self::ReplayPending => "Garmin projections are being rebuilt",
            Self::Database => "Daily metrics could not be read",
            Self::InvalidTimezone => "The analysis timezone is invalid",
            Self::InvalidKnowledgeCutoff => "As-known cutoff cannot follow the event",
            Self::SleepRange => "Sleep analysis requires at most 31 calendar days",
            Self::SleepNapLimit => "Sleep analysis exceeds 200 naps; narrow the interval",
            Self::SleepSessionLimit => {
                "Nap intervals span too many main sessions; inspect their dates"
            }
            Self::TemporalVersionLimit => {
                "Temporal context exceeds 100000 versions; narrow the activity range"
            }
        })
    }
}

impl Error for HealthAnalysisError {}

impl From<sqlx::Error> for HealthAnalysisError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

#[derive(Clone, Copy)]
pub struct HealthPeriod {
    start: Date,
    end: Date,
}

impl HealthPeriod {
    pub fn new(start: Date, end: Date) -> Result<Self, HealthAnalysisError> {
        check_calendar_bounds(start, end)?;
        if !(0..=MAX_RANGE_DAYS).contains(&(end - start).whole_days()) {
            return Err(HealthAnalysisError::DateRange);
        }
        Ok(Self { start, end })
    }

    fn days(self) -> i64 {
        (self.end - self.start).whole_days() + 1
    }
}

fn check_calendar_bounds(start: Date, end: Date) -> Result<(), HealthAnalysisError> {
    let earliest =
        Date::from_calendar_date(1, Month::March, 2).map_err(|_| HealthAnalysisError::DateRange)?;
    let latest = Date::from_calendar_date(9999, Month::November, 1)
        .map_err(|_| HealthAnalysisError::DateRange)?;
    if start < earliest || end > latest {
        return Err(HealthAnalysisError::CalendarBoundary);
    }
    Ok(())
}

impl Database {
    pub async fn personal_health_baseline(
        &self,
        metric: &str,
        period: HealthPeriod,
    ) -> Result<Value, HealthAnalysisError> {
        daily_metric(metric)?;
        self.verified_health_reader()
            .await?
            .personal_baseline(metric, period)
            .await
    }

    pub async fn compare_health_periods(
        &self,
        metric: &str,
        first: HealthPeriod,
        second: HealthPeriod,
    ) -> Result<Value, HealthAnalysisError> {
        if first.start.max(second.start) <= first.end.min(second.end) {
            return Err(HealthAnalysisError::Overlap);
        }
        daily_metric(metric)?;
        self.verified_health_reader()
            .await?
            .compare_periods(metric, first, second)
            .await
    }
}

impl DailyHealthReader {
    pub async fn personal_baseline(
        mut self,
        metric: &str,
        period: HealthPeriod,
    ) -> Result<Value, HealthAnalysisError> {
        let (metric, query) = daily_metric(metric)?;
        let values = observed_values(&mut self.0, query, period).await?;
        self.0.commit().await?;
        let description = describe(values.iter().map(|(_, value)| Some(*value)));
        Ok(period_document(metric, period, &values, description))
    }

    pub async fn compare_periods(
        mut self,
        metric: &str,
        first: HealthPeriod,
        second: HealthPeriod,
    ) -> Result<Value, HealthAnalysisError> {
        if first.start.max(second.start) <= first.end.min(second.end) {
            return Err(HealthAnalysisError::Overlap);
        }
        let (metric, query) = daily_metric(metric)?;
        let first_values = observed_values(&mut self.0, query, first).await?;
        let second_values = observed_values(&mut self.0, query, second).await?;
        self.0.commit().await?;
        let first_description = describe(first_values.iter().map(|(_, value)| Some(*value)));
        let second_description = describe(second_values.iter().map(|(_, value)| Some(*value)));
        let first_samples: Vec<_> = first_values.iter().map(|(_, value)| *value).collect();
        let second_samples: Vec<_> = second_values.iter().map(|(_, value)| *value).collect();
        let estimate = block_mean_difference(&first_samples, &second_samples);
        Ok(json!({
            "metric": metric,
            "a": period_document(metric, first, &first_values, first_description),
            "b": period_document(metric, second, &second_values, second_description),
            "difference": estimate.difference, "ci95": estimate.ci95, "method": estimate.method,
            "standardized_difference": standardized_difference(estimate.difference, first_description, second_description),
            "limitations": [
                "Observational association; not evidence of causation",
                "Missing days can bias estimates; blocks follow observations, not calendar gaps",
                "No adjustment for multiple comparisons or unmeasured confounders",
            ],
        }))
    }
}

pub(crate) async fn owner_timezone(
    txn: &mut Transaction<'_, Postgres>,
    configured: &str,
) -> Result<String, HealthAnalysisError> {
    let selected: Option<String> = sqlx::query_scalar(
        "SELECT timezone FROM people
         WHERE EXISTS(SELECT 1 FROM app_state WHERE key='preferences:onboarding')",
    )
    .fetch_optional(&mut **txn)
    .await?;
    Ok(selected.unwrap_or_else(|| configured.to_owned()))
}

async fn begin_verified_analysis(
    pool: &PgPool,
) -> Result<Transaction<'static, Postgres>, HealthAnalysisError> {
    let mut txn = begin_daily_analysis(pool).await?;
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut *txn)
        .await?;
    if !locked {
        return Err(HealthAnalysisError::Storage(ReadinessError::Unavailable));
    }
    if projection_pending(&mut txn, PARSER_VERSION).await? {
        return Err(HealthAnalysisError::ReplayPending);
    }
    Ok(txn)
}

async fn begin_daily_analysis(
    pool: &PgPool,
) -> Result<Transaction<'static, Postgres>, HealthAnalysisError> {
    let mut txn = pool
        .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        .await?;
    sqlx::query("SET LOCAL statement_timeout = '750ms'")
        .execute(&mut *txn)
        .await?;
    check_readiness(&mut txn)
        .await
        .map_err(HealthAnalysisError::Storage)?;
    Ok(txn)
}

async fn observed_values(
    txn: &mut Transaction<'_, Postgres>,
    query: &'static str,
    period: HealthPeriod,
) -> Result<Vec<(Date, f64)>, HealthAnalysisError> {
    Ok(sqlx::query_as(query)
        .bind(period.start)
        .bind(period.end)
        .bind(period.days())
        .fetch_all(&mut **txn)
        .await?)
}

fn period_document(
    metric: &str,
    period: HealthPeriod,
    values: &[(Date, f64)],
    stats: ga_domain::statistics::Description,
) -> Value {
    let days: Vec<_> = values.iter().map(|(day, _)| day.to_string()).collect();
    json!({
        "metric": metric, "start": period.start.to_string(), "end": period.end.to_string(),
        "n": stats.n, "mean": stats.mean, "median": stats.median, "sd": stats.sd,
        "min": stats.min, "max": stats.max, "observed_dates": days,
        "missing_days": period.days() - values.len() as i64,
        "method": "observed daily values; missing days are not zero",
    })
}

fn daily_metric(metric: &str) -> Result<(&'static str, &'static str), HealthAnalysisError> {
    macro_rules! select_daily_metric {
        ($($column:literal),+ $(,)?) => {
            match metric {
                $($column => Ok(($column, concat!(
                    "SELECT day, ", $column, "::double precision FROM health_days ",
                    "WHERE day BETWEEN $1 AND $2 AND ", $column, " IS NOT NULL ",
                    "ORDER BY day LIMIT $3"
                ))),)+
                _ => Err(HealthAnalysisError::UnknownMetric),
            }
        };
    }
    select_daily_metric!(
        "hydration_ml",
        "sleep_score",
        "sleep_seconds",
        "deep_seconds",
        "rem_seconds",
        "light_seconds",
        "awake_seconds",
        "hrv_nightly_avg",
        "hrv_weekly_avg",
        "hrv_baseline_low",
        "hrv_baseline_high",
        "resting_hr",
        "body_battery_high",
        "body_battery_low",
        "body_battery_charged",
        "body_battery_drained",
        "stress_avg",
        "stress_max",
        "steps",
        "active_calories",
        "intensity_minutes",
        "training_readiness_score",
        "recovery_time_minutes",
        "vo2max",
    )
}
