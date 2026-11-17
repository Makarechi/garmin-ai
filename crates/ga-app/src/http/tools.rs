use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::health_analysis::{HealthAnalysisError, HealthPeriod};
use ga_domain::access::{ReadToolInput, Scopes};
use serde_json::{Map, Value, json};
use speedate::{DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig};
use time::{Date, Month};

use super::{AppState, method_not_allowed, private_cache};

const MAX_REQUEST_BYTES: usize = 1024 * 1024;
const REPLAY_NOTICE: &str = "Данные Garmin пересчитываются после изменения версии обработки. Анализ временно недоступен; это не означает отсутствие данных. Проверьте /status позже.";

#[derive(Clone, Copy)]
enum HealthTool {
    Snapshot,
    Range,
}

impl HealthTool {
    const IMPLEMENTED: [Self; 2] = [Self::Snapshot, Self::Range];

    fn name(self) -> &'static str {
        match self {
            Self::Snapshot => "health_snapshot",
            Self::Range => "health_range",
        }
    }

    fn catalog_entry(self) -> Value {
        let date = |title| json!({"format": "date", "title": title, "type": "string"});
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
        };
        json!({
            "name": self.name(),
            "description": description,
            "input_schema": {
                "title": title, "type": "object", "additionalProperties": false,
                "properties": properties, "required": required,
            },
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
        let visible: Vec<_> = HealthTool::IMPLEMENTED
            .into_iter()
            .filter(|tool| tool.permits(scopes))
            .map(HealthTool::catalog_entry)
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
        execute_health_tool(state, &parts.headers, &name, body).await
    } else {
        method_not_allowed("POST")
    };
    private_cache(response, &parts.headers)
}

async fn execute_health_tool(
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
    let Some(tool) = HealthTool::IMPLEMENTED
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
    let reader = match database.verified_health_reader().await {
        Ok(reader) => reader,
        Err(error) => return health_failure(error),
    };
    let outcome = match tool {
        HealthTool::Snapshot if arguments.len() == 1 => {
            match arguments.get("day").and_then(date_input) {
                Some(day) => reader.snapshot(day).await,
                None => return invalid_arguments(),
            }
        }
        HealthTool::Range if arguments.len() == 2 => {
            let (Some(start), Some(end)) = (
                arguments.get("start").and_then(date_input),
                arguments.get("end").and_then(date_input),
            ) else {
                return invalid_arguments();
            };
            match HealthPeriod::new(start, end) {
                Ok(period) => reader.range(period).await,
                Err(error) => Err(error),
            }
        }
        _ => return invalid_arguments(),
    };
    match outcome {
        Ok(document) => Json(document).into_response(),
        Err(error) => health_failure(error),
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

fn detail(status: StatusCode, detail: &'static str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}

fn unauthorized() -> Response {
    detail(StatusCode::UNAUTHORIZED, "Authentication required")
}

fn forbidden() -> Response {
    detail(StatusCode::FORBIDDEN, "Insufficient scope")
}

fn invalid_arguments() -> Response {
    detail(StatusCode::UNPROCESSABLE_ENTITY, "Invalid arguments")
}

fn storage_unavailable() -> Response {
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
        HealthAnalysisError::CalendarBoundary | HealthAnalysisError::DateRange => {
            invalid_arguments()
        }
        _ => storage_unavailable(),
    }
}
