use axum::{
    Json,
    body::to_bytes,
    extract::{Path, Request, State},
    http::{HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use ga_db::diary::actions::{FormSubmission, FormSubmitError, TrackerActionError};
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

pub(super) async fn form(
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
            Ok((database, owner)) => database
                .tracker_generated_form(
                    owner,
                    &id,
                    request_locale(&uri).as_deref(),
                    &state.configured_locale,
                )
                .await
                .map_or_else(action_failure, |form| Json(form).into_response()),
            Err(status) => access_failure(status),
        }
    };
    private_cache(response, &headers)
}

pub(super) async fn submit(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    let (head, body) = request.into_parts();
    let result = if head.method != Method::POST {
        method_not_allowed("POST")
    } else {
        match authorized(
            &state,
            &head.headers,
            &[Scope::ReadDiary, Scope::WriteDiary],
        )
        .await
        {
            Ok((database, owner)) => {
                let submitted = async {
                    let bytes = to_bytes(body, super::tools::MAX_REQUEST_BYTES)
                        .await
                        .map_err(|_| FormSubmitError::Invalid)?;
                    let form = FormSubmission::parse(&bytes)?;
                    database.submit_tracker_form(owner, &id, &form).await
                }
                .await;
                match submitted {
                    Ok(stored) => super::diary::event_response(stored.snapshot),
                    Err(error) => submit_failure(error),
                }
            }
            Err(status) => access_failure(status),
        }
    };
    private_cache(result, &head.headers)
}

fn submit_failure(error: FormSubmitError) -> Response {
    match error {
        FormSubmitError::Action(error) => action_failure(error),
        FormSubmitError::Diary(error) => super::diary::write_failure(error),
        FormSubmitError::Invalid => detail(StatusCode::UNPROCESSABLE_ENTITY, "Invalid arguments"),
        FormSubmitError::Conflict(message) => detail(StatusCode::CONFLICT, message),
        FormSubmitError::Validation(issues) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail":"Form validation failed","errors":issues})),
        )
            .into_response(),
    }
}

fn action_failure(error: TrackerActionError) -> Response {
    let status = match error {
        TrackerActionError::Missing => StatusCode::NOT_FOUND,
        TrackerActionError::InactiveContract | TrackerActionError::EntryChanged => {
            StatusCode::CONFLICT
        }
        TrackerActionError::InvalidLocale => StatusCode::UNPROCESSABLE_ENTITY,
        TrackerActionError::Diary(_) | TrackerActionError::InvalidContract => {
            StatusCode::SERVICE_UNAVAILABLE
        }
    };
    (status, Json(json!({"detail":error.to_string()}))).into_response()
}
