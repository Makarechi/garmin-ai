use ga_db::{
    Database,
    health_analysis::{ActivityReadError, DeviceHistoryError, HealthAnalysisError, HealthPeriod},
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value};
use time::Date;
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

pub(crate) async fn read_garmin(
    database: &Database,
    tool: ReadTool,
    arguments: &Map<String, Value>,
    mcp_owner: Option<Uuid>,
) -> Result<Value, GarminReadError> {
    let mut reader = database.verified_health_reader().await?;
    let query = parse(tool, arguments)?;
    if let Some(owner) = mcp_owner
        && !reader.permits_model_health(owner).await?
    {
        return Err(GarminReadError::Permission);
    }
    match query {
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
    }
}

enum Query<'a> {
    Snapshot(Date),
    Range(Date, Date),
    Activities(StoredInstant, StoredInstant, Option<&'a str>),
    Activity(&'a str, bool, u32, u16),
    Devices(StoredInstant, StoredInstant, u16),
    Baseline(&'a str, Date, Date),
    Compare(&'a str, [Date; 4]),
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
        _ => Err(GarminReadError::Arguments),
    }
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
