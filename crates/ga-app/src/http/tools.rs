use std::io::{self, Read};

use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_app::read_tool_catalog::ReadTool;
use ga_archive::json_spool::JsonDocument;
use ga_db::{
    diary::DiaryReadError,
    health_analysis::{HealthAnalysisError, NapPolicy},
};
use ga_domain::{access::ReadToolInput, timestamp::StoredInstant};
use serde_json::{Map, Value, json};
use speedate::{DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig};
use time::{Date, Month, OffsetDateTime};

use super::{AppState, method_not_allowed, private_cache};

pub(super) const MAX_REQUEST_BYTES: usize = 1024 * 1024;
const REPLAY_NOTICE: &str = "Данные Garmin пересчитываются после изменения версии обработки. Анализ временно недоступен; это не означает отсутствие данных. Проверьте /status позже.";

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
    if tool == ReadTool::GenericAnalysis {
        use ga_db::health_analysis::generic::GenericAudience;
        let spec = match generic_input(&arguments) {
            Ok(spec) => spec,
            Err(error) => return generic_reads::validation_failure(&arguments, error),
        };
        if !scopes.permits_tool(
            tool.name(),
            ReadToolInput::Analysis {
                operation: spec.document()["operation"].as_str().unwrap_or_default(),
                metric: spec.document()["metric_key"].as_str(),
            },
        ) {
            return forbidden();
        }
        let result = async {
            let reader = database
                .generic_analysis_reader(GenericAudience::Owner {
                    read_health: scopes.permits(&[ga_domain::access::Scope::ReadHealth]),
                })
                .await?;
            reader.read(&spec).await
        }
        .await;
        return match result {
            Ok(document) => document_response(document),
            Err(error) => generic_failure(error),
        };
    }
    if matches!(tool, ReadTool::ResourceCatalog) {
        let Ok(page) = catalog_input(&arguments) else {
            return invalid_arguments();
        };
        if !scopes.permits_tool(tool.name(), ReadToolInput::Catalog(page.kind.name())) {
            return forbidden();
        }
        return match database
            .resource_catalog(
                page,
                None,
                scopes.permits(&[ga_domain::access::Scope::ReadHealth]),
            )
            .await
        {
            Ok(document) => document_response(document),
            Err(ga_db::resource_catalog::CatalogError::Health(error)) => health_failure(error),
            Err(ga_db::resource_catalog::CatalogError::Arguments) => invalid_arguments(),
            Err(ga_db::resource_catalog::CatalogError::ModelConsent) => forbidden(),
            Err(ga_db::resource_catalog::CatalogError::Database) => storage_unavailable(),
        };
    }
    if matches!(tool, ReadTool::EventDefinitions) {
        let Ok(page) = definition_input(&arguments) else {
            return invalid_arguments();
        };
        return match database.event_definitions(page, None).await {
            Ok(document) => document_response(document),
            Err(error) => diary_failure(error),
        };
    }
    if matches!(tool, ReadTool::Events) {
        let Some((start, end, kind)) = range_with_kind(&arguments) else {
            return invalid_arguments();
        };
        return match database.list_owner_events(start, end, kind, 500).await {
            Ok(document) => Json(document).into_response(),
            Err(error) => diary_failure(error),
        };
    }
    if matches!(tool, ReadTool::WellbeingObservations) {
        let Some((start, end, cursor)) = wellbeing_input(&arguments) else {
            return invalid_arguments();
        };
        return match database
            .wellbeing_observations(start, end, cursor, None)
            .await
        {
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
        return match read_garmin(
            &database,
            tool,
            &arguments,
            None,
            &state.configured_timezone,
        )
        .await
        {
            Ok(value) => document_response(value),
            Err(GarminReadError::Missing) => detail(StatusCode::NOT_FOUND, "Record not found"),
            Err(GarminReadError::Health(error)) => health_failure(error),
            Err(_) => invalid_arguments(),
        };
    }
    invalid_arguments()
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
    ga_domain::legacy_json::integer_argument(value)?
        .try_into()
        .ok()
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

pub(crate) fn wellbeing_input(
    arguments: &Map<String, Value>,
) -> Option<(StoredInstant, StoredInstant, Option<&str>)> {
    if arguments
        .keys()
        .any(|key| !matches!(key.as_str(), "start" | "end" | "cursor"))
    {
        return None;
    }
    let start = StoredInstant::from_legacy_input(arguments.get("start")?)?;
    let end = StoredInstant::from_legacy_input(arguments.get("end")?)?;
    let cursor = match arguments.get("cursor") {
        None | Some(Value::Null) => None,
        Some(Value::String(cursor)) => Some(cursor.as_str()),
        _ => return None,
    };
    Some((start, end, cursor))
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

pub(super) fn health_failure(error: HealthAnalysisError) -> Response {
    match error {
        HealthAnalysisError::TemporalCalendarOverflow => {
            (StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error").into_response()
        }
        HealthAnalysisError::ReplayPending => (
            StatusCode::SERVICE_UNAVAILABLE,
            [(header::RETRY_AFTER, "60")],
            Json(json!({"detail": REPLAY_NOTICE})),
        )
            .into_response(),
        HealthAnalysisError::CalendarBoundary
        | HealthAnalysisError::DateRange
        | HealthAnalysisError::UnknownMetric
        | HealthAnalysisError::InvalidTimezone
        | HealthAnalysisError::InvalidTemporalTimestamp
        | HealthAnalysisError::InvalidKnowledgeCutoff
        | HealthAnalysisError::TemporalVersionLimit
        | HealthAnalysisError::Overlap
        | HealthAnalysisError::SleepRange
        | HealthAnalysisError::SleepNapLimit
        | HealthAnalysisError::MigraineEpisodeLimit
        | HealthAnalysisError::CoffeeInputLimit
        | HealthAnalysisError::CoffeeEvidenceLimit
        | HealthAnalysisError::InsightArguments
        | HealthAnalysisError::SleepSessionLimit => invalid_arguments(),
        _ => storage_unavailable(),
    }
}
fn generic_failure(error: ga_db::health_analysis::generic::GenericAnalysisError) -> Response {
    use ga_db::health_analysis::generic::GenericAnalysisError;
    match error {
        GenericAnalysisError::Lookup(_) => detail(StatusCode::NOT_FOUND, "Record not found"),
        GenericAnalysisError::Value(_) | GenericAnalysisError::Encoding => invalid_arguments(),
        GenericAnalysisError::Calculation(error) if error.class_name() == "ValueError" => {
            invalid_arguments()
        }
        GenericAnalysisError::Permission => forbidden(),
        GenericAnalysisError::Health(error) => health_failure(error),
        GenericAnalysisError::Database | GenericAnalysisError::Data => storage_unavailable(),
        _ => (StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error").into_response(),
    }
}
mod garmin_reads;
mod generic_reads;
pub(crate) use generic_reads::{generic_error_class, generic_input};

pub(crate) use garmin_reads::{GarminReadError, read_garmin};

pub(super) fn document_response(document: JsonDocument) -> Response {
    let bytes = document.byte_len();
    let chunks = futures_util::stream::try_unfold(document.reader(), |mut reader| async move {
        let (reader, chunk) = tokio::task::spawn_blocking(move || {
            let mut chunk = vec![0; 64 * 1024];
            let count = reader.read(&mut chunk)?;
            chunk.truncate(count);
            Ok::<_, io::Error>((reader, chunk))
        })
        .await
        .map_err(io::Error::other)??;
        Ok::<_, io::Error>((!chunk.is_empty()).then_some((chunk, reader)))
    });
    let mut response = Body::from_stream(chunks).into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    response
        .headers_mut()
        .insert(header::CONTENT_LENGTH, HeaderValue::from(bytes));
    response
}

pub(crate) fn definition_input(
    arguments: &Map<String, Value>,
) -> Result<ga_db::diary::DefinitionPage<'_>, &'static str> {
    if arguments.keys().any(|key| {
        !matches!(
            key.as_str(),
            "after_key" | "definition_key" | "before_version" | "limit"
        )
    }) {
        return Err("ValidationError");
    }
    let text = |key| match arguments.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.as_str())),
        _ => Err("ValidationError"),
    };
    let before_version = match arguments.get("before_version") {
        None | Some(Value::Null) => None,
        Some(value) => Some(
            ga_domain::legacy_json::integer_argument(value)
                .ok_or("ValidationError")?
                .to_string(),
        ),
    };
    let limit = match arguments.get("limit") {
        None => 10,
        Some(value) => ga_domain::legacy_json::integer_argument(value)
            .ok_or("ValidationError")?
            .try_into()
            .unwrap_or(0),
    };
    Ok(ga_db::diary::DefinitionPage {
        after_key: text("after_key")?,
        definition_key: text("definition_key")?,
        before_version,
        limit,
    })
}

pub(crate) fn catalog_input(
    arguments: &Map<String, Value>,
) -> Result<ga_db::resource_catalog::ResourcePage<'_>, &'static str> {
    use ga_db::resource_catalog::{ResourceKind, ResourcePage};
    if arguments.keys().any(|key| {
        !matches!(
            key.as_str(),
            "kind" | "after_key" | "resource_key" | "limit" | "detail"
        )
    }) {
        return Err("ValidationError");
    }
    let kind = match arguments.get("kind").and_then(Value::as_str) {
        Some("events") => ResourceKind::Events,
        Some("system_metrics") => ResourceKind::SystemMetrics,
        Some("tracker_metrics") => ResourceKind::TrackerMetrics,
        Some("raw_sources") => ResourceKind::RawSources,
        _ => return Err("ValidationError"),
    };
    let text = |key| match arguments.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if value.chars().count() <= 200 => Ok(Some(value.as_str())),
        _ => Err("ValidationError"),
    };
    let limit = match arguments.get("limit") {
        None => 10,
        Some(value) => ga_domain::legacy_json::integer_argument(value)
            .ok_or("ValidationError")?
            .try_into()
            .unwrap_or(0),
    };
    let detail = match arguments.get("detail") {
        None => false,
        Some(value) => bool_input(value).ok_or("ValidationError")?,
    };
    Ok(ResourcePage {
        kind,
        after_key: text("after_key")?,
        resource_key: text("resource_key")?,
        limit,
        detail,
    })
}
