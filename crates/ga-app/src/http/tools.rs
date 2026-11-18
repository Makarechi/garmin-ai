use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::{
    diary::DiaryReadError,
    health_analysis::{HealthAnalysisError, NapPolicy, TimelineError},
};
use ga_domain::{
    access::{ReadToolInput, Scopes},
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use speedate::{DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig};
use time::{Date, Month, OffsetDateTime};

use super::{AppState, method_not_allowed, private_cache};

pub(super) const MAX_REQUEST_BYTES: usize = 1024 * 1024;
const REPLAY_NOTICE: &str = "Данные Garmin пересчитываются после изменения версии обработки. Анализ временно недоступен; это не означает отсутствие данных. Проверьте /status позже.";

#[derive(Clone, Copy, Eq, PartialEq)]
pub(crate) enum ReadTool {
    Snapshot,
    Range,
    Activities,
    ActivityDetails,
    Events,
    Timeline,
    DeviceHistory,
    Freshness,
    Baseline,
    ComparePeriods,
    RunningEfficiency,
    MetricSeries,
    LaggedAssociation,
    Sleep,
}

impl ReadTool {
    pub(crate) const GARMIN_READS: [Self; 10] = [
        Self::Snapshot,
        Self::Range,
        Self::Activities,
        Self::ActivityDetails,
        Self::DeviceHistory,
        Self::Baseline,
        Self::ComparePeriods,
        Self::RunningEfficiency,
        Self::MetricSeries,
        Self::LaggedAssociation,
    ];
    const IMPLEMENTED: [Self; 14] = [
        Self::Snapshot,
        Self::Range,
        Self::Activities,
        Self::ActivityDetails,
        Self::Events,
        Self::Timeline,
        Self::Freshness,
        Self::Baseline,
        Self::ComparePeriods,
        Self::RunningEfficiency,
        Self::MetricSeries,
        Self::LaggedAssociation,
        Self::Sleep,
        Self::DeviceHistory,
    ];

    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Snapshot => "health_snapshot",
            Self::Range => "health_range",
            Self::Activities => "activities",
            Self::ActivityDetails => "activity_details",
            Self::Events => "events",
            Self::Timeline => "timeline",
            Self::DeviceHistory => "device_history",
            Self::Freshness => "data_freshness",
            Self::Baseline => "personal_baseline",
            Self::ComparePeriods => "analysis_compare_periods",
            Self::RunningEfficiency => "analysis_running_efficiency",
            Self::MetricSeries => "metric_series",
            Self::LaggedAssociation => "analysis_lagged_association",
            Self::Sleep => "analysis_sleep",
        }
    }

    pub(crate) fn catalog_entry(self) -> Value {
        let date = |title| json!({"format": "date", "title": title, "type": "string"});
        let datetime = |title| json!({"format": "date-time", "title": title, "type": "string"});
        let typed_range = || {
            json!({
                "start": datetime("Start"),
                "end": datetime("End"),
                "kind": {"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Kind"}
            })
        };
        let (description, title, properties, required) = match self {
            Self::Snapshot => (
                "Daily Garmin values and explicit missing metrics for one local date.",
                "health_snapshotArgs",
                json!({"day": date("Day")}),
                &["day"][..],
            ),
            Self::Range => (
                "Stored daily summaries for an inclusive date range.",
                "health_rangeArgs",
                json!({"start": date("Start"), "end": date("End")}),
                &["start", "end"][..],
            ),
            Self::Activities => (
                "List stored activity summaries in a half-open timestamp range.",
                "activitiesArgs",
                typed_range(),
                &["start", "end"][..],
            ),
            Self::ActivityDetails => (
                "Details, laps and HR zones for one activity. Raw samples are excluded by default. Use next_offset to continue and a smaller limit for bulky samples.",
                "activity_detailsArgs",
                json!({
                    "activity_id":{"title":"Activity Id","type":"string"},
                    "include_samples":{"default":false,"title":"Include Samples","type":"boolean"},
                    "offset":{"default":0,"title":"Offset","type":"integer"},
                    "limit":{"default":100,"title":"Limit","type":"integer"},
                }),
                &["activity_id"][..],
            ),
            Self::Events => (
                "Read confirmed, inferred, or pending diary events with their status; excludes deleted records.",
                "eventsArgs",
                typed_range(),
                &["start", "end"][..],
            ),
            Self::Timeline => (
                "Overlapping sleep, activity, wellbeing, context and plan layers with evidence. Segment labels are a legacy display projection; annotations preserve overlaps. Points cover no duration; calendar plans do not prove attendance. At most 500 annotations in 31 days.",
                "timelineArgs",
                json!({"start": datetime("Start"), "end": datetime("End")}),
                &["start", "end"][..],
            ),
            Self::DeviceHistory => (
                "Bounded activity-scoped FIT device and historical zone evidence; no serial numbers or inferred sensor attribution.",
                "device_historyArgs",
                json!({
                    "start": datetime("Start"), "end": datetime("End"),
                    "limit": {"default":100,"title":"Limit","type":"integer"},
                }),
                &["start", "end"][..],
            ),
            Self::Baseline => (
                "Compute a personal baseline from available daily values with sample size and missing days.",
                "personal_baselineArgs",
                json!({"metric":{"type":"string","title":"Metric"},"start":date("Start"),"end":date("End")}),
                &["metric", "start", "end"][..],
            ),
            Self::ComparePeriods => (
                "Compare non-overlapping daily periods with block-bootstrap uncertainty when enough observations exist.",
                "analysis_compare_periodsArgs",
                json!({"metric":{"type":"string","title":"Metric"},"a_start":date("A Start"),"a_end":date("A End"),
                       "b_start":date("B Start"),"b_end":date("B End")}),
                &["metric", "a_start", "a_end", "b_start", "b_end"][..],
            ),
            Self::RunningEfficiency => (
                "Describe runs chronologically within the requested HR range, separating activity types; missing route/weather/sensor/RPE evidence prevents physiological ranking.",
                "analysis_running_efficiencyArgs",
                json!({"start":datetime("Start"),"end":datetime("End"),
                       "hr_min":{"type":"number","title":"Hr Min","default":0},
                       "hr_max":{"type":"number","title":"Hr Max","default":250}}),
                &["start", "end"][..],
            ),
            Self::MetricSeries => (
                "Intraday gauges use coverage-gated time-weighted means; steps_bucket uses sum. Returns metric contract and source provenance. Hydration is a daily summary in health_snapshot, not an intraday drink event. Range is half-open.",
                "metric_seriesArgs",
                json!({"metric":{"type":"string","title":"Metric"},"start":datetime("Start"),
                    "end":datetime("End"),"minutes":{"type":"integer","title":"Minutes","default":5}}),
                &["metric", "start", "end"][..],
            ),
            Self::Sleep => (
                "Main sleep timestamps in rows[].main_interval, stages and naps for at most 31 days. Dates select Garmin's wake-day summary: today's sleep may start yesterday evening. timing_only=true requires start=end and nap_policy=separate; it returns exactly one verified main sleep interval without waiting for unrelated history replay. For a multi-day pattern or summary use timing_only=false. Missing nap logs are unknown; no recovery or prediction claims.",
                "analysis_sleepArgs",
                json!({
                    "start":date("Start"),"end":date("End"),
                    "nap_policy":{"type":"string","enum":["separate","include_confirmed"],"title":"Nap Policy","default":"separate"},
                    "timing_only":{"type":"boolean","title":"Timing Only","default":false}
                }),
                &["start", "end"][..],
            ),
            Self::LaggedAssociation => (
                "Exploratory daily correlations at specified calendar-day lags; association is not causation.",
                "analysis_lagged_associationArgs",
                json!({"metric_a":{"title":"Metric A","type":"string"},
                    "metric_b":{"title":"Metric B","type":"string"},
                    "start":date("Start"),"end":date("End"),
                    "lags":{"title":"Lags","type":"array","items":{"type":"integer"}}}),
                &["metric_a", "metric_b", "start", "end", "lags"][..],
            ),
            Self::Freshness => (
                "Separate API fetch times, observed-signal lag, coverage and current-state quality gates.",
                "data_freshnessArgs",
                json!({}),
                &[][..],
            ),
        };
        let mut input_schema = json!({
            "title": title, "type": "object", "additionalProperties": false,
            "properties": properties,
        });
        if !required.is_empty() {
            input_schema["required"] = json!(required);
        }
        json!({
            "name": self.name(),
            "description": description,
            "input_schema": input_schema,
        })
    }

    fn permits(self, scopes: Scopes) -> bool {
        scopes.permits_tool(self.name(), ReadToolInput::Discovery)
    }
}

pub(super) async fn list(
    method: Method,
    headers: HeaderMap,
    State(state): State<AppState>,
) -> Response {
    let response = if method != Method::GET {
        method_not_allowed("GET")
    } else if let Some(scopes) = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
    {
        let visible: Vec<_> = ReadTool::IMPLEMENTED
            .into_iter()
            .filter(|tool| tool.permits(scopes))
            .map(ReadTool::catalog_entry)
            .collect();
        Json(visible).into_response()
    } else {
        unauthorized()
    };
    private_cache(response, &headers)
}

pub(super) async fn execute(
    State(state): State<AppState>,
    Path(name): Path<String>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let response = if parts.method == Method::POST {
        execute_read_tool(state, &parts.headers, &name, body).await
    } else {
        method_not_allowed("POST")
    };
    private_cache(response, &parts.headers)
}

async fn execute_read_tool(
    state: AppState,
    headers: &HeaderMap,
    name: &str,
    body: Body,
) -> Response {
    let Some(scopes) = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
    else {
        return unauthorized();
    };
    let Some(tool) = ReadTool::IMPLEMENTED
        .into_iter()
        .find(|tool| tool.name() == name)
    else {
        return forbidden();
    };
    if !tool.permits(scopes) {
        return forbidden();
    }
    let Some(database) = state.database else {
        return storage_unavailable();
    };
    let arguments = match arguments(body).await {
        Ok(arguments) => arguments,
        Err(error) => return error.into_response(),
    };
    if matches!(tool, ReadTool::Events) {
        let Some((start, end, kind)) = range_with_kind(&arguments) else {
            return invalid_arguments();
        };
        return match database.list_owner_events(start, end, kind, 500).await {
            Ok(document) => Json(document).into_response(),
            Err(error) => diary_failure(error),
        };
    }
    if matches!(tool, ReadTool::Freshness) {
        if !arguments.is_empty() {
            return invalid_arguments();
        }
        let micros = OffsetDateTime::now_utc()
            .unix_timestamp_nanos()
            .div_euclid(1000);
        let Ok(micros) = i64::try_from(micros) else {
            return storage_unavailable();
        };
        let Ok(at) = StoredInstant::from_unix_microseconds(micros) else {
            return storage_unavailable();
        };
        return match database
            .owner_data_freshness(at, &state.configured_timezone)
            .await
        {
            Ok(document) => Json(document).into_response(),
            Err(error) => health_failure(error),
        };
    }
    if matches!(tool, ReadTool::Sleep) && arguments.get("timing_only") == Some(&Value::Bool(true)) {
        let Some((start, end, NapPolicy::Separate, true)) = sleep_input(&arguments) else {
            return invalid_arguments();
        };
        if start != end {
            return invalid_arguments();
        }
        return match database
            .owner_main_sleep_timing(start, &state.configured_timezone)
            .await
        {
            Ok(value) => Json(value).into_response(),
            Err(error) => health_failure(error),
        };
    }
    if ReadTool::GARMIN_READS.contains(&tool) {
        return match read_garmin(&database, tool, &arguments, None).await {
            Ok(value) => Json(value).into_response(),
            Err(GarminReadError::Missing) => detail(StatusCode::NOT_FOUND, "Record not found"),
            Err(GarminReadError::Health(error)) => health_failure(error),
            Err(_) => invalid_arguments(),
        };
    }
    let reader = match database.verified_health_reader().await {
        Ok(reader) => reader,
        Err(error) => return health_failure(error),
    };
    let outcome = match tool {
        ReadTool::Sleep => {
            let Some((start, end, policy, timing_only)) = sleep_input(&arguments) else {
                return invalid_arguments();
            };
            if timing_only {
                if start != end || policy != NapPolicy::Separate {
                    return invalid_arguments();
                }
                reader
                    .owner_main_sleep_timing(start, &state.configured_timezone)
                    .await
            } else {
                reader
                    .owner_sleep_analysis(start, end, &state.configured_timezone, policy)
                    .await
            }
        }
        ReadTool::Timeline if arguments.len() == 2 => {
            let (Some(start), Some(end)) = (
                arguments
                    .get("start")
                    .and_then(StoredInstant::legacy_input_with_offset),
                arguments
                    .get("end")
                    .and_then(StoredInstant::legacy_input_with_offset),
            ) else {
                return invalid_arguments();
            };
            return match reader.owner_timeline(start, end).await {
                Ok(document) => {
                    ([(header::CONTENT_TYPE, "application/json")], document).into_response()
                }
                Err(TimelineError::Health(error)) => health_failure(error),
                Err(TimelineError::Diary(error)) => diary_failure(error),
                Err(TimelineError::Database) => storage_unavailable(),
                Err(_) => invalid_arguments(),
            };
        }
        _ => return invalid_arguments(),
    };
    match outcome {
        Ok(document) => Json(document).into_response(),
        Err(error) => health_failure(error),
    }
}

fn device_arguments(arguments: &Map<String, Value>) -> Option<(StoredInstant, StoredInstant, u16)> {
    if arguments
        .keys()
        .any(|key| !matches!(key.as_str(), "start" | "end" | "limit"))
    {
        return None;
    }
    let limit = arguments.get("limit").map_or(Some(100), integer_input)?;
    Some((
        StoredInstant::from_legacy_input(arguments.get("start")?)?,
        StoredInstant::from_legacy_input(arguments.get("end")?)?,
        u16::try_from(limit).ok()?,
    ))
}

pub(crate) fn range_with_kind(
    arguments: &Map<String, Value>,
) -> Option<(StoredInstant, StoredInstant, Option<&str>)> {
    if arguments
        .keys()
        .any(|key| !matches!(key.as_str(), "start" | "end" | "kind"))
    {
        return None;
    }
    let kind = match arguments.get("kind") {
        None | Some(Value::Null) => None,
        Some(Value::String(kind)) => Some(kind.as_str()),
        _ => return None,
    };
    Some((
        StoredInstant::from_legacy_input(arguments.get("start")?)?,
        StoredInstant::from_legacy_input(arguments.get("end")?)?,
        kind,
    ))
}

fn diary_failure(error: DiaryReadError) -> Response {
    match error {
        DiaryReadError::Storage(_) | DiaryReadError::Database | DiaryReadError::InvalidEvent => {
            storage_unavailable()
        }
        _ => invalid_arguments(),
    }
}

fn bool_input(value: &Value) -> Option<bool> {
    match value {
        Value::Bool(value) => Some(*value),
        Value::Number(_) => match value.as_f64()? {
            0.0 => Some(false),
            1.0 => Some(true),
            _ => None,
        },
        Value::String(value) => match value.to_ascii_lowercase().as_str() {
            "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
            "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
            _ => None,
        },
        _ => None,
    }
}

pub(super) fn integer_input(value: &Value) -> Option<u32> {
    match value {
        Value::Bool(value) => Some(u32::from(*value)),
        Value::Number(_) => {
            let value = value.as_f64()?;
            (value.fract() == 0.0 && (0.0..=f64::from(u32::MAX)).contains(&value))
                .then_some(value as u32)
        }
        Value::String(value) => {
            let value = value.trim();
            let integer = match value.split_once('.') {
                Some((integer, fraction))
                    if !fraction.is_empty() && fraction.bytes().all(|b| b == b'0') =>
                {
                    integer
                }
                Some(_) => return None,
                None => value,
            };
            let content = integer.strip_prefix(['+', '-']).unwrap_or(integer);
            if !content.as_bytes().first().is_some_and(u8::is_ascii_digit)
                || !content.as_bytes().last().is_some_and(u8::is_ascii_digit)
                || content.contains("__")
            {
                return None;
            }
            let number = content.replace('_', "").parse::<u32>().ok()?;
            (!integer.starts_with('-') || number == 0).then_some(number)
        }
        _ => None,
    }
}

enum BodyError {
    Missing,
    InvalidJson,
    Limit,
    Validation(Vec<Value>),
}

impl IntoResponse for BodyError {
    fn into_response(self) -> Response {
        match self {
            Self::Missing => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail": [{"type":"missing","loc":["body"],"msg":"Field required","input":null}]})),
            ).into_response(),
            Self::InvalidJson => invalid_arguments(),
            Self::Limit => detail(StatusCode::PAYLOAD_TOO_LARGE, "Tool request exceeds the size limit"),
            Self::Validation(errors) => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail": errors})),
            ).into_response(),
        }
    }
}

async fn arguments(body: Body) -> Result<Map<String, Value>, BodyError> {
    let data = to_bytes(body, MAX_REQUEST_BYTES)
        .await
        .map_err(|_| BodyError::Limit)?;
    if data.is_empty() {
        return Err(BodyError::Missing);
    }
    let data = data.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&data);
    let document: Value = serde_json::from_slice(data).map_err(|_| BodyError::InvalidJson)?;
    let Value::Object(mut document) = document else {
        return Err(BodyError::Validation(vec![json!({
            "type": "model_attributes_type", "loc": ["body"],
            "msg": "Input should be a valid dictionary or object to extract fields from",
            "input": document,
        })]));
    };
    let mut errors = Vec::new();
    let arguments = match document.remove("arguments") {
        Some(Value::Object(arguments)) => arguments,
        None => Map::new(),
        Some(other) => {
            errors.push(json!({
                "type": "dict_type", "loc": ["body", "arguments"],
                "msg": "Input should be a valid dictionary", "input": other,
            }));
            Map::new()
        }
    };
    errors.extend(document.into_iter().map(|(key, value)| {
        json!({
            "type": "extra_forbidden", "loc": ["body", key],
            "msg": "Extra inputs are not permitted", "input": value,
        })
    }));
    if errors.is_empty() {
        Ok(arguments)
    } else {
        Err(BodyError::Validation(errors))
    }
}

fn sleep_input(arguments: &Map<String, Value>) -> Option<(Date, Date, NapPolicy, bool)> {
    if arguments
        .keys()
        .any(|key| !matches!(key.as_str(), "start" | "end" | "nap_policy" | "timing_only"))
    {
        return None;
    }
    let start = date_input(arguments.get("start")?)?;
    let end = date_input(arguments.get("end")?)?;
    let policy = match arguments
        .get("nap_policy")
        .map_or(Some("separate"), Value::as_str)?
    {
        "separate" => NapPolicy::Separate,
        "include_confirmed" => NapPolicy::IncludeConfirmed,
        _ => return None,
    };
    let timing_only = arguments
        .get("timing_only")
        .map_or(Some(false), bool_input)?;
    Some((start, end, policy, timing_only))
}

fn date_input(value: &Value) -> Option<Date> {
    let bytes = match value {
        Value::String(text) => text.as_bytes(),
        Value::Number(number) => number.as_str().as_bytes(),
        _ => return None,
    };
    let parsed = speedate::Date::parse_bytes(bytes).ok().or_else(|| {
        let config = DateTimeConfig {
            time_config: TimeConfig {
                microseconds_precision_overflow_behavior:
                    MicrosecondsPrecisionOverflowBehavior::Truncate,
                ..TimeConfig::default()
            },
            ..DateTimeConfig::default()
        };
        let stamp = DateTime::parse_bytes_with_config(bytes, &config).ok()?;
        (stamp.time.hour == 0
            && stamp.time.minute == 0
            && stamp.time.second == 0
            && stamp.time.microsecond == 0)
            .then_some(stamp.date)
    })?;
    if parsed.year == 0 {
        return None;
    }
    Date::from_calendar_date(
        i32::from(parsed.year),
        Month::try_from(parsed.month).ok()?,
        parsed.day,
    )
    .ok()
}

pub(super) fn detail(status: StatusCode, detail: &'static str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}

pub(super) fn unauthorized() -> Response {
    detail(StatusCode::UNAUTHORIZED, "Authentication required")
}

pub(super) fn forbidden() -> Response {
    detail(StatusCode::FORBIDDEN, "Insufficient scope")
}

pub(super) fn invalid_arguments() -> Response {
    detail(StatusCode::UNPROCESSABLE_ENTITY, "Invalid arguments")
}

pub(super) fn storage_unavailable() -> Response {
    detail(
        StatusCode::SERVICE_UNAVAILABLE,
        "Database unavailable or identity is not ready",
    )
}

fn health_failure(error: HealthAnalysisError) -> Response {
    match error {
        HealthAnalysisError::ReplayPending => (
            StatusCode::SERVICE_UNAVAILABLE,
            [(header::RETRY_AFTER, "60")],
            Json(json!({"detail": REPLAY_NOTICE})),
        )
            .into_response(),
        HealthAnalysisError::CalendarBoundary
        | HealthAnalysisError::DateRange
        | HealthAnalysisError::UnknownMetric
        | HealthAnalysisError::Overlap
        | HealthAnalysisError::SleepRange
        | HealthAnalysisError::SleepNapLimit
        | HealthAnalysisError::SleepSessionLimit => invalid_arguments(),
        _ => storage_unavailable(),
    }
}
mod garmin_reads;

pub(crate) use garmin_reads::{GarminReadError, read_garmin};
