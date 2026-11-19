use std::collections::BTreeMap;

use axum::{
    Json,
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{IntoResponse, Response},
};
use ga_db::diary::DiaryReadError;
use ga_domain::{
    access::Scope,
    legacy_json::{self, Utf8Layout},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use speedate::{DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig};
use strum::EnumMessage;

use super::{
    AppState, method_not_allowed, private_cache,
    tools::{forbidden, invalid_arguments, storage_unavailable, unauthorized},
};

pub(super) async fn diary(State(state): State<AppState>, request: Request) -> Response {
    let (parts, _) = request.into_parts();
    let response = if parts.method == Method::GET {
        export(state, &parts.headers, &parts.uri).await
    } else {
        method_not_allowed("GET")
    };
    private_cache(response, &parts.headers)
}

async fn export(state: AppState, headers: &HeaderMap, uri: &Uri) -> Response {
    let Some(scopes) = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
    else {
        return unauthorized();
    };
    if !scopes.permits(&[Scope::ReadDiary]) {
        return forbidden();
    }
    let parameters: BTreeMap<_, _> =
        form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect();
    let mut errors = Vec::new();
    let start = instant(
        parameters.get("start").map(|value| value.as_ref()),
        "start",
        &mut errors,
    );
    let end = instant(
        parameters.get("end").map(|value| value.as_ref()),
        "end",
        &mut errors,
    );
    let format = parameters
        .get("format")
        .map_or("json", |value| value.as_ref());
    if !matches!(format, "json" | "csv") {
        let expected = "'json' or 'csv'";
        errors.push(json!({
            "type":"literal_error","loc":["query","format"],
            "msg":format!("Input should be {expected}"),"input":format,"ctx":{"expected":expected}
        }));
    }
    let (Some(start), Some(end), true) = (start, end, errors.is_empty()) else {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail":errors})),
        )
            .into_response();
    };
    let Some(database) = state.database else {
        return storage_unavailable();
    };
    let timezone = parameters.get("timezone").map(|value| value.as_ref());
    let result = match database
        .export_diary(start, end, timezone, &state.configured_timezone)
        .await
    {
        Ok(result) => result,
        Err(
            DiaryReadError::TimestampRange
            | DiaryReadError::CalendarRange
            | DiaryReadError::CalendarBoundary
            | DiaryReadError::InvalidZone
            | DiaryReadError::Limit,
        ) => {
            return invalid_arguments();
        }
        Err(_) => return storage_unavailable(),
    };
    let (disposition, response) = if format == "csv" {
        let Some(body) = csv(&result) else {
            return storage_unavailable();
        };
        (
            r#"attachment; filename="garmin-diary.csv""#,
            ([(header::CONTENT_TYPE, "text/csv; charset=utf-8")], body).into_response(),
        )
    } else {
        (
            r#"attachment; filename="garmin-diary.json""#,
            Json(result).into_response(),
        )
    };
    (
        [
            (header::CACHE_CONTROL, "no-store"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
            (header::CONTENT_DISPOSITION, disposition),
        ],
        response,
    )
        .into_response()
}

pub(super) fn instant(
    value: Option<&str>,
    field: &str,
    errors: &mut Vec<Value>,
) -> Option<StoredInstant> {
    let Some(value) = value else {
        errors.push(
            json!({"type":"missing","loc":["query",field],"msg":"Field required","input":null}),
        );
        return None;
    };
    if let Some(at) = StoredInstant::from_legacy_input(&json!(value)) {
        return Some(at);
    }
    let config = DateTimeConfig {
        time_config: TimeConfig {
            microseconds_precision_overflow_behavior:
                MicrosecondsPrecisionOverflowBehavior::Truncate,
            ..TimeConfig::default()
        },
        ..DateTimeConfig::default()
    };
    let date_time = DateTime::parse_bytes_with_config(value.as_bytes(), &config);
    if date_time
        .as_ref()
        .is_ok_and(|value| value.time.tz_offset.is_none())
        || speedate::Date::parse_bytes(value.as_bytes()).is_ok()
    {
        errors.push(json!({
            "type":"timezone_aware","loc":["query",field],
            "msg":"Input should have timezone info","input":value
        }));
    } else {
        let description = date_time
            .err()
            .and_then(|error| error.get_documentation())
            .unwrap_or("timestamp is outside the supported calendar");
        errors.push(json!({
            "type":"datetime_from_date_parsing","loc":["query",field],"input":value,
            "msg":format!("Input should be a valid datetime or date, {description}"),
            "ctx":{"error":description}
        }));
    }
    None
}

const CSV_FIELDS: &[&str] = &[
    "id",
    "kind",
    "display_start",
    "display_end",
    "display_timezone",
    "start",
    "end",
    "timezone",
    "topology",
    "missing_end",
    "status",
    "source",
    "confidence",
    "revision",
    "definition_version_id",
    "payload_json",
];

fn csv(result: &Value) -> Option<String> {
    let mut output = String::new();
    csv_row(&mut output, CSV_FIELDS.iter().copied());
    let zone = result["display_timezone"].as_str()?;
    for entry in result["rows"].as_array()? {
        let bytes = serde_json::to_vec(&entry["payload"]).ok()?;
        let payload =
            String::from_utf8(legacy_json::encode(&bytes, Utf8Layout::Archive, 2_000_000).ok()?)
                .ok()?;
        let mut values = Vec::with_capacity(CSV_FIELDS.len());
        for field in CSV_FIELDS {
            use std::borrow::Cow::{Borrowed, Owned};
            values.push(match *field {
                "display_timezone" => Borrowed(zone),
                "payload_json" => Borrowed(payload.as_str()),
                field => match &entry[field] {
                    Value::Null => Borrowed(""),
                    Value::Bool(true) => Borrowed("True"),
                    Value::Bool(false) => Borrowed("False"),
                    Value::String(value) => Borrowed(value.as_str()),
                    Value::Number(value) => Owned(value.to_string()),
                    _ => return None,
                },
            });
        }
        csv_row(&mut output, values.iter().map(|part| part.as_ref()));
    }
    Some(output)
}

fn csv_row<'a>(output: &mut String, values: impl Iterator<Item = &'a str>) {
    for (index, value) in values.enumerate() {
        if index > 0 {
            output.push(',');
        }
        output.push('"');
        if value
            .trim_start_matches(|part: char| {
                part.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&part)
            })
            .starts_with(['=', '+', '-', '@'])
            || value.starts_with(['\t', '\r', '\n'])
        {
            output.push('\'');
        }
        for character in value.chars() {
            if character == '"' {
                output.push('"');
            }
            output.push(character);
        }
        output.push('"');
    }
    output.push_str("\r\n");
}
