use std::collections::BTreeMap;

use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{Method, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use ga_db::{
    Database,
    calendar_context::{CalendarBatch, CalendarError, CalendarSources},
};
use ga_domain::access::Scope;
use serde_json::{Value, json};
use uuid::Uuid;

use super::{
    AppState,
    diary::{access_failure, authorized},
    exports::instant,
    method_not_allowed, private_cache,
    tools::{MAX_REQUEST_BYTES, detail, invalid_arguments, storage_unavailable},
};
use crate::configuration;

pub(super) async fn plans(State(state): State<AppState>, request: Request) -> Response {
    handle(state, request, "GET").await
}

pub(super) async fn import(State(state): State<AppState>, request: Request) -> Response {
    handle(state, request, "POST").await
}

async fn handle(state: AppState, request: Request, method: &'static str) -> Response {
    let (parts, body) = request.into_parts();
    let response = if parts.method != method {
        method_not_allowed(method)
    } else {
        match authorized(&state, &parts.headers, &[Scope::Admin]).await {
            Ok((database, owner)) if parts.method == Method::POST => {
                import_items(database, owner, body).await
            }
            Ok((database, owner)) => list(database, owner, &parts.uri).await,
            Err(status) => access_failure(status),
        }
    };
    private_cache(response, &parts.headers)
}

async fn list(database: &Database, owner: Uuid, uri: &Uri) -> Response {
    let query = uri.query().unwrap_or_default();
    let parameters: BTreeMap<_, _> = form_urlencoded::parse(query.as_bytes()).collect();
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
    let (Some(start), Some(end)) = (start, end) else {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail":errors})),
        )
            .into_response();
    };
    let sources = match sources() {
        Ok(sources) => sources,
        Err(error) => return failure(error),
    };
    match database.calendar_plans(owner, &sources, start, end).await {
        Ok(report) => Json(report).into_response(),
        Err(error) => failure(error),
    }
}

async fn import_items(database: &Database, owner: Uuid, body: Body) -> Response {
    let bytes = match to_bytes(body, MAX_REQUEST_BYTES).await {
        Ok(bytes) => bytes,
        Err(_) => {
            return detail(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Calendar batch exceeds the size limit",
            );
        }
    };
    let bytes = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
    let Ok(value) = serde_json::from_slice::<Value>(bytes) else {
        return invalid_arguments();
    };
    let batch = match CalendarBatch::parse(value) {
        Ok(batch) => batch,
        Err(error) => return failure(error),
    };
    let sources = match sources() {
        Ok(sources) => sources,
        Err(error) => return failure(error),
    };
    match database
        .import_calendar_plans(owner, &sources, &batch)
        .await
    {
        Ok(report) => Json(report).into_response(),
        Err(error) => failure(error),
    }
}

fn sources() -> Result<CalendarSources, CalendarError> {
    let [text] =
        configuration::read(["GA_CALENDAR_SOURCES"]).map_err(|_| CalendarError::Database)?;
    let configured = text.as_deref().unwrap_or("[]");
    if configured.len() > MAX_REQUEST_BYTES {
        return Err(CalendarError::Invalid);
    }
    CalendarSources::parse(configured)
}

fn failure(error: CalendarError) -> Response {
    match error {
        CalendarError::Invalid => invalid_arguments(),
        CalendarError::Conflict => detail(
            StatusCode::CONFLICT,
            "Calendar revision was reused with different content",
        ),
        CalendarError::Database => storage_unavailable(),
    }
}
