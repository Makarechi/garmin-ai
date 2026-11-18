use axum::{
    Json,
    extract::{Path, State},
    http::{HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use ga_db::diary::actions::TrackerActionError;
use ga_domain::access::Scope;
use serde_json::json;
use uuid::Uuid;

use super::{
    AppState,
    diary::{access_failure, authorized},
    method_not_allowed, private_cache,
    tools::detail,
};

fn request_locale(uri: &Uri) -> Option<String> {
    form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(name, _)| name == "locale")
        .last()
        .map(|(_, locale)| locale.into_owned())
}

pub(super) async fn available(
    method: Method,
    headers: HeaderMap,
    State(state): State<AppState>,
    uri: Uri,
) -> Response {
    let response = if method != Method::GET {
        method_not_allowed("GET")
    } else {
        match authorized(&state, &headers, &[Scope::ReadDiary]).await {
            Ok((database, owner)) => database
                .available_tracker_actions(
                    owner,
                    request_locale(&uri).as_deref(),
                    &state.configured_locale,
                )
                .await
                .map_or_else(action_failure, |rows| Json(rows).into_response()),
            Err(status) => access_failure(status),
        }
    };
    private_cache(response, &headers)
}

pub(super) async fn event(
    method: Method,
    headers: HeaderMap,
    State(state): State<AppState>,
    Path(id): Path<String>,
    uri: Uri,
) -> Response {
    let response = if method != Method::GET {
        method_not_allowed("GET")
    } else {
        match authorized(&state, &headers, &[Scope::ReadDiary]).await {
            Ok((database, owner)) => match Uuid::parse_str(&id) {
                Ok(event) => database
                    .tracker_action_for_event(
                        owner,
                        event,
                        request_locale(&uri).as_deref(),
                        &state.configured_locale,
                    )
                    .await
                    .map_or_else(action_failure, |action| Json(action).into_response()),
                Err(_) => detail(StatusCode::UNPROCESSABLE_ENTITY, "Invalid event identifier"),
            },
            Err(status) => access_failure(status),
        }
    };
    private_cache(response, &headers)
}

fn action_failure(error: TrackerActionError) -> Response {
    let status = match error {
        TrackerActionError::Missing => StatusCode::NOT_FOUND,
        TrackerActionError::InvalidLocale => StatusCode::UNPROCESSABLE_ENTITY,
        TrackerActionError::Diary(_) | TrackerActionError::InvalidContract => {
            StatusCode::SERVICE_UNAVAILABLE
        }
    };
    (status, Json(json!({"detail":error.to_string()}))).into_response()
}
