use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_db::{
    Database,
    health_analysis::{
        ActivityReadError, DeviceHistoryError, HealthAnalysisError, HealthPeriod,
        MODEL_HEALTH_PACKS, SeriesReadError, model_metric_packs,
    },
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{ReadTool, bool_input, date_input, device_arguments, integer_input, range_with_kind};

pub(crate) enum GarminReadError {
    Arguments,
    Missing,
    Permission,
    Health(HealthAnalysisError),
    Value,
}

impl From<HealthAnalysisError> for GarminReadError {
    fn from(error: HealthAnalysisError) -> Self {
        Self::Health(error)
    }
}

impl From<ActivityReadError> for GarminReadError {
    fn from(error: ActivityReadError) -> Self {
        match error {
            ActivityReadError::Missing => Self::Missing,
            ActivityReadError::Analysis(error) => Self::Health(error),
            _ => Self::Value,
        }
    }
}

impl From<SeriesReadError> for GarminReadError {
    fn from(error: SeriesReadError) -> Self {
        match error {
            SeriesReadError::Analysis(error) => Self::Health(error),
            _ => Self::Value,
        }
    }
}

pub(crate) async fn read_garmin(
    database: &Database,
    tool: ReadTool,
    arguments: &Map<String, Value>,
    mcp_owner: Option<Uuid>,
    configured_timezone: &str,
) -> Result<JsonDocument, GarminReadError> {
    let mut reader = database.verified_health_reader().await?;
    let query = parse(tool, arguments)?;
    if let Some(owner) = mcp_owner
        && (!reader.permits_model_health(owner).await?
            || !reader
                .permits_model_packs(owner, query.model_packs())
                .await?)
    {
        return Err(GarminReadError::Permission);
    }
    if let (Some(owner), Query::Lagged(_, other, ..)) = (mcp_owner, &query)
        && !reader
            .permits_model_packs(owner, model_metric_packs(other))
            .await?
    {
        return Err(GarminReadError::Permission);
    }
    if mcp_owner.is_some() {
        let kind = match &query {
            Query::EventWindows(kind, ..) => Some(*kind),
            Query::Migraine(..) => Some("migraine"),
            _ => None,
        };
        if let Some(kind) = kind
            && !reader.permits_model_event_kind(kind).await?
        {
            return Err(GarminReadError::Permission);
        }
    }
    let value = match query {
        Query::Snapshot(day) => Ok(reader.snapshot(day).await?),
        Query::Range(start, end) => Ok(reader.range(HealthPeriod::new(start, end)?).await?),
        Query::Activities(start, end, kind) => {
            Ok(reader.activity_summaries(start, end, kind, 200).await?)
        }
        Query::Activity(id, samples, offset, limit) => {
            Ok(reader.activity_details(id, samples, offset, limit).await?)
        }
        Query::Devices(start, end, limit) => reader
            .device_history(start, end, limit)
            .await
            .map_err(|error| match error {
                DeviceHistoryError::Analysis(error) => GarminReadError::Health(error),
                _ => GarminReadError::Value,
            }),
        Query::Baseline(metric, start, end) => Ok(reader
            .personal_baseline(metric, HealthPeriod::new(start, end)?)
            .await?),
        Query::Compare(metric, [a_start, a_end, b_start, b_end]) => Ok(reader
            .compare_periods(
                metric,
                HealthPeriod::new(a_start, a_end)?,
                HealthPeriod::new(b_start, b_end)?,
            )
            .await?),
        Query::Running(start, end, min, max) => {
            Ok(reader.running_efficiency(start, end, min, max).await?)
        }
        Query::Lagged(first, second, start, end, lags) => Ok(reader
            .lagged_association(first, second, HealthPeriod::new(start, end)?, &lags)
            .await?),
        Query::EventWindows(kind, metric, start, end) => {
            return Ok(reader.event_windows(kind, metric, start, end).await?);
        }
        Query::Stress(start, end, timezone) => Ok(reader
            .stress_hours(start, end, timezone, configured_timezone)
            .await?),
        Query::Migraine(metric, start, end, timezone) => Ok(reader
            .migraine_comparison(
                metric,
                HealthPeriod::new(start, end)?,
                timezone,
                configured_timezone,
            )
            .await?),
        Query::Series(metric, start, end, minutes) => {
            Ok(reader.metric_series(metric, start, end, minutes).await?)
        }
    }?;
    let mut document = JsonSpool::default();
    serde_json::to_writer(&mut document, &value)
        .map_err(|_| GarminReadError::Health(HealthAnalysisError::Database))?;
    drop(value);
    document
        .finish()
        .map_err(|_| GarminReadError::Health(HealthAnalysisError::Database))
}

enum Query<'a> {
    Snapshot(Date),
    Range(Date, Date),
    Activities(StoredInstant, StoredInstant, Option<&'a str>),
    Activity(&'a str, bool, u32, u16),
    Devices(StoredInstant, StoredInstant, u16),
    Baseline(&'a str, Date, Date),
    Compare(&'a str, [Date; 4]),
    Running(StoredInstant, StoredInstant, f64, f64),
    Series(&'a str, OffsetDateTime, OffsetDateTime, u16),
    Stress(Date, Date, Option<&'a str>),
    Lagged(&'a str, &'a str, Date, Date, Vec<i64>),
    EventWindows(&'a str, &'a str, OffsetDateTime, OffsetDateTime),
    Migraine(&'a str, Date, Date, Option<&'a str>),
}

impl Query<'_> {
    fn model_packs(&self) -> &'static [&'static str] {
        match self {
            Self::Snapshot(_) | Self::Range(..) => MODEL_HEALTH_PACKS,
            Self::Activities(..) | Self::Activity(..) | Self::Devices(..) => &["training"],
            Self::Baseline(metric, ..)
            | Self::Compare(metric, ..)
            | Self::Series(metric, ..)
            | Self::Lagged(metric, ..)
            | Self::Migraine(metric, ..)
            | Self::EventWindows(_, metric, ..) => model_metric_packs(metric),
            Self::Running(..) => &["training", "sleep", "wellbeing"],
            Self::Stress(..) => &["wellbeing"],
        }
    }
}

fn parse(tool: ReadTool, arguments: &Map<String, Value>) -> Result<Query<'_>, GarminReadError> {
    let day = |name| arguments.get(name).and_then(date_input);
    let metric = || arguments.get("metric").and_then(Value::as_str);
    match tool {
        ReadTool::Snapshot if arguments.len() == 1 => {
            let day = arguments
                .get("day")
                .and_then(date_input)
                .ok_or(GarminReadError::Arguments)?;
            Ok(Query::Snapshot(day))
        }
        ReadTool::Range if arguments.len() == 2 => {
            let start = arguments
                .get("start")
                .and_then(date_input)
                .ok_or(GarminReadError::Arguments)?;
            let end = arguments
                .get("end")
                .and_then(date_input)
                .ok_or(GarminReadError::Arguments)?;
            Ok(Query::Range(start, end))
        }
        ReadTool::Activities => {
            let (start, end, kind) =
                range_with_kind(arguments).ok_or(GarminReadError::Arguments)?;
            Ok(Query::Activities(start, end, kind))
        }
        ReadTool::ActivityDetails => {
            let (id, samples, offset, limit) = activity_arguments(arguments)?;
            Ok(Query::Activity(id, samples, offset, limit))
        }
        ReadTool::DeviceHistory => {
            let (start, end, limit) =
                device_arguments(arguments).ok_or(GarminReadError::Arguments)?;
            Ok(Query::Devices(start, end, limit))
        }
        ReadTool::Baseline if arguments.len() == 3 => {
            let (Some(metric), Some(start), Some(end)) = (metric(), day("start"), day("end"))
            else {
                return Err(GarminReadError::Arguments);
            };
            Ok(Query::Baseline(metric, start, end))
        }
        ReadTool::ComparePeriods if arguments.len() == 5 => {
            let Some(metric) = metric() else {
                return Err(GarminReadError::Arguments);
            };
            let [Some(a_start), Some(a_end), Some(b_start), Some(b_end)] =
                ["a_start", "a_end", "b_start", "b_end"].map(day)
            else {
                return Err(GarminReadError::Arguments);
            };
            Ok(Query::Compare(metric, [a_start, a_end, b_start, b_end]))
        }
        ReadTool::RunningEfficiency => running_arguments(arguments),
        ReadTool::MetricSeries => series_arguments(arguments),
        ReadTool::StressHours
            if arguments
                .keys()
                .all(|key| matches!(key.as_str(), "start" | "end" | "timezone")) =>
        {
            let (Some(start), Some(end)) = (day("start"), day("end")) else {
                return Err(GarminReadError::Arguments);
            };
            let timezone = match arguments.get("timezone") {
                None | Some(Value::Null) => None,
                Some(Value::String(value)) => Some(value.as_str()),
                _ => return Err(GarminReadError::Arguments),
            };
            Ok(Query::Stress(start, end, timezone))
        }
        ReadTool::MigraineWindows
            if arguments
                .keys()
                .all(|key| matches!(key.as_str(), "metric" | "start" | "end" | "timezone")) =>
        {
            let (Some(metric), Some(start), Some(end)) = (metric(), day("start"), day("end"))
            else {
                return Err(GarminReadError::Arguments);
            };
            let timezone = match arguments.get("timezone") {
                None | Some(Value::Null) => None,
                Some(Value::String(value)) => Some(value.as_str()),
                _ => return Err(GarminReadError::Arguments),
            };
            Ok(Query::Migraine(metric, start, end, timezone))
        }
        ReadTool::LaggedAssociation if arguments.len() == 5 => {
            let first = arguments
                .get("metric_a")
                .and_then(Value::as_str)
                .ok_or(GarminReadError::Arguments)?;
            let second = arguments
                .get("metric_b")
                .and_then(Value::as_str)
                .ok_or(GarminReadError::Arguments)?;
            let (Some(start), Some(end)) = (day("start"), day("end")) else {
                return Err(GarminReadError::Arguments);
            };
            let values = arguments
                .get("lags")
                .and_then(Value::as_array)
                .filter(|values| values.len() <= 15)
                .ok_or(GarminReadError::Arguments)?;
            let lags = values
                .iter()
                .map(lag_input)
                .collect::<Option<Vec<_>>>()
                .ok_or(GarminReadError::Arguments)?;
            Ok(Query::Lagged(first, second, start, end, lags))
        }
        ReadTool::EventWindows if arguments.len() == 4 => {
            let requested = arguments
                .get("event_type")
                .and_then(Value::as_str)
                .ok_or(GarminReadError::Arguments)?;
            let kind = requested.strip_prefix("system.").unwrap_or(requested);
            let metric = metric().ok_or(GarminReadError::Arguments)?;
            let start = arguments
                .get("start")
                .and_then(StoredInstant::legacy_input_with_offset)
                .ok_or(GarminReadError::Arguments)?;
            let end = arguments
                .get("end")
                .and_then(StoredInstant::legacy_input_with_offset)
                .ok_or(GarminReadError::Arguments)?;
            Ok(Query::EventWindows(kind, metric, start, end))
        }
        _ => Err(GarminReadError::Arguments),
    }
}

fn series_arguments(arguments: &Map<String, Value>) -> Result<Query<'_>, GarminReadError> {
    if arguments
        .keys()
        .any(|key| !matches!(key.as_str(), "metric" | "start" | "end" | "minutes"))
    {
        return Err(GarminReadError::Arguments);
    }
    let metric = arguments
        .get("metric")
        .and_then(Value::as_str)
        .ok_or(GarminReadError::Arguments)?;
    let start = arguments
        .get("start")
        .and_then(StoredInstant::legacy_input_with_offset)
        .ok_or(GarminReadError::Arguments)?;
    let end = arguments
        .get("end")
        .and_then(StoredInstant::legacy_input_with_offset)
        .ok_or(GarminReadError::Arguments)?;
    let minutes = arguments
        .get("minutes")
        .map_or(Some(5), integer_input)
        .and_then(|value| u16::try_from(value).ok())
        .ok_or(GarminReadError::Arguments)?;
    Ok(Query::Series(metric, start, end, minutes))
}

fn running_arguments(arguments: &Map<String, Value>) -> Result<Query<'_>, GarminReadError> {
    if arguments
        .keys()
        .any(|key| !matches!(key.as_str(), "start" | "end" | "hr_min" | "hr_max"))
    {
        return Err(GarminReadError::Arguments);
    }
    let clock = |name| {
        arguments
            .get(name)
            .and_then(StoredInstant::from_legacy_input)
    };
    let rate = |name, default| {
        arguments
            .get(name)
            .map_or(Some(default), |value| match value {
                Value::String(text) => text.trim().parse().ok(),
                Value::Bool(value) => Some(f64::from(u8::from(*value))),
                value => value.as_f64(),
            })
    };
    let (Some(start), Some(end), Some(min), Some(max)) = (
        clock("start"),
        clock("end"),
        rate("hr_min", 0.0),
        rate("hr_max", 250.0),
    ) else {
        return Err(GarminReadError::Arguments);
    };
    Ok(Query::Running(start, end, min, max))
}

fn activity_arguments(
    arguments: &Map<String, Value>,
) -> Result<(&str, bool, u32, u16), GarminReadError> {
    if arguments.keys().any(|key| {
        !matches!(
            key.as_str(),
            "activity_id" | "include_samples" | "offset" | "limit"
        )
    }) {
        return Err(GarminReadError::Arguments);
    }
    let (Some(id), Some(samples), Some(offset), Some(limit)) = (
        arguments.get("activity_id").and_then(Value::as_str),
        arguments
            .get("include_samples")
            .map_or(Some(false), bool_input),
        arguments.get("offset").map_or(Some(0), integer_input),
        arguments.get("limit").map_or(Some(100), integer_input),
    ) else {
        return Err(GarminReadError::Arguments);
    };
    let limit = u16::try_from(limit).map_err(|_| GarminReadError::Value)?;
    Ok((id, samples, offset, limit))
}

fn lag_input(value: &Value) -> Option<i64> {
    let parsed = match value {
        Value::Number(_) if value.as_f64()? < 0.0 => {
            integer_input(&serde_json::json!(-value.as_f64()?)).map(|number| -i64::from(number))
        }
        Value::String(text) if text.trim().starts_with('-') => {
            let magnitude = text.trim().strip_prefix('-')?;
            if magnitude.starts_with(['+', '-']) || magnitude != magnitude.trim() {
                return None;
            }
            integer_input(&Value::String(magnitude.to_owned())).map(|number| -i64::from(number))
        }
        _ => integer_input(value).map(i64::from),
    }?;
    (-30..=30).contains(&parsed).then_some(parsed)
}
