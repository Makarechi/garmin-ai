use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::{
    Database,
    diary::{
        CreateSystemEvent, DiaryWriteError,
        custom_entries::{CreateCustomEntry, UpdateCustomEntry},
        event_document,
    },
};
use ga_domain::{
    access::Scope,
    event_input::{EventInput, ValidationMode},
    event_replay::{ReplayConflict, check_idempotency_key},
    event_schema::CustomEntry,
};
use uuid::Uuid;

use super::{
    AppState, method_not_allowed, private_cache,
    tools::{
        MAX_REQUEST_BYTES, detail, forbidden, integer_input, invalid_arguments,
        storage_unavailable, unauthorized,
    },
};

#[derive(Clone, Copy)]
enum EntryKind {
    System,
    Tracker,
}

pub(super) async fn create(State(state): State<AppState>, request: Request) -> Response {
    create_request(&state, request, EntryKind::System).await
}

pub(super) async fn create_custom(State(state): State<AppState>, request: Request) -> Response {
    create_request(&state, request, EntryKind::Tracker).await
}

pub(super) async fn edit_custom(
    Path(id): Path<String>,
    State(state): State<AppState>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let response = if parts.method == Method::PUT {
        match authorized(
            &state,
            &parts.headers,
            &[Scope::ReadDiary, Scope::WriteDiary],
        )
        .await
        {
            Ok((database, owner)) => match id.parse() {
                Ok(id) => update_tracker_entry(database, owner, id, body).await,
                Err(_) => invalid_arguments(),
            },
            Err(status) => access_failure(status),
        }
    } else {
        method_not_allowed("PUT")
    };
    private_cache(response, &parts.headers)
}

async fn update_tracker_entry(database: &Database, owner: Uuid, id: Uuid, body: Body) -> Response {
    let Ok(bytes) = to_bytes(body, MAX_REQUEST_BYTES).await else {
        return report_too_large();
    };
    let bytes = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
    let Ok(serde_json::Value::Object(mut body)) = serde_json::from_slice(bytes) else {
        return invalid_arguments();
    };
    let Some(revision) = body
        .get("revision")
        .and_then(integer_input)
        .filter(|revision| *revision >= 1)
    else {
        return invalid_arguments();
    };
    let Some(entry) = body
        .remove("entry")
        .and_then(|entry| CustomEntry::from_value(entry).ok())
    else {
        return invalid_arguments();
    };
    match database
        .update_custom_entry_document(UpdateCustomEntry {
            owner_id: owner,
            actor: "api",
            id,
            expected_revision: i32::try_from(revision).unwrap_or(0),
            entry: &entry,
            evidence_refs: &[],
        })
        .await
    {
        Ok(value) => Json(value).into_response(),
        Err(error) => write_failure(error),
    }
}

async fn create_request(state: &AppState, request: Request, kind: EntryKind) -> Response {
    let (parts, body) = request.into_parts();
    let response = match parts.method {
        Method::POST => {
            match authorized(
                state,
                &parts.headers,
                &[Scope::ReadDiary, Scope::WriteDiary],
            )
            .await
            {
                Ok((database, owner)) => {
                    create_entry(database, owner, &parts.headers, body, kind).await
                }
                Err(status) => access_failure(status),
            }
        }
        _ => method_not_allowed("POST"),
    };
    private_cache(response, &parts.headers)
}

pub(super) async fn entry(
    Path(id): Path<String>,
    State(state): State<AppState>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    let response = if method == Method::GET {
        match authorized(&state, &headers, &[Scope::ReadDiary]).await {
            Ok((database, owner)) => match id.parse() {
                Ok(id) => match database.owner_event(owner, id).await {
                    Ok(Some(value)) => Json(value).into_response(),
                    Ok(None) => detail(StatusCode::NOT_FOUND, "Record not found"),
                    Err(_) => storage_unavailable(),
                },
                Err(_) => invalid_arguments(),
            },
            Err(status) => access_failure(status),
        }
    } else {
        method_not_allowed("GET")
    };
    private_cache(response, &headers)
}

async fn create_entry(
    database: &Database,
    owner: Uuid,
    headers: &HeaderMap,
    body: Body,
    kind: EntryKind,
) -> Response {
    let idempotency = headers.get("idempotency-key").map(|key| {
        key.as_bytes()
            .iter()
            .map(|byte| char::from(*byte))
            .collect::<String>()
    });
    if idempotency
        .as_deref()
        .is_some_and(|key| check_idempotency_key(key).is_err())
    {
        return invalid_arguments();
    }
    let body = match to_bytes(body, MAX_REQUEST_BYTES).await {
        Ok(data) => data,
        Err(_) => return report_too_large(),
    };
    let body = body.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&body);
    let written = match kind {
        EntryKind::System => {
            let Ok(report) =
                EventInput::from_json(body, MAX_REQUEST_BYTES, ValidationMode::NewReport)
            else {
                return invalid_arguments();
            };
            database
                .create_system_event(CreateSystemEvent {
                    owner_id: owner,
                    authenticated_actor: "api",
                    report: &report,
                    idempotency_key: idempotency.as_deref(),
                    operation_id: None,
                    clock_uncertainty_seconds: None,
                })
                .await
        }
        EntryKind::Tracker => {
            let Ok(entry) = CustomEntry::from_json(body, MAX_REQUEST_BYTES) else {
                return invalid_arguments();
            };
            database
                .create_custom_entry(CreateCustomEntry {
                    owner_id: owner,
                    actor: "api",
                    entry: &entry,
                    idempotency_key: idempotency.as_deref(),
                    evidence_refs: &[],
                })
                .await
        }
    };
    match written {
        Ok(entry) => match event_document(entry.snapshot) {
            Ok(value) => Json(value).into_response(),
            Err(_) => storage_unavailable(),
        },
        Err(error) => write_failure(error),
    }
}

async fn authorized<'a>(
    state: &'a AppState,
    headers: &HeaderMap,
    required: &[Scope],
) -> Result<(&'a Database, Uuid), StatusCode> {
    let scopes = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
        .ok_or(StatusCode::UNAUTHORIZED)?;
    if !scopes.permits(required) {
        return Err(StatusCode::FORBIDDEN);
    }
    let database = state
        .database
        .as_ref()
        .ok_or(StatusCode::SERVICE_UNAVAILABLE)?;
    let owner = database
        .enrolled_owner_id()
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    Ok((database, owner))
}

fn access_failure(status: StatusCode) -> Response {
    match status {
        StatusCode::UNAUTHORIZED => unauthorized(),
        StatusCode::FORBIDDEN => forbidden(),
        _ => storage_unavailable(),
    }
}

fn report_too_large() -> Response {
    detail(
        StatusCode::PAYLOAD_TOO_LARGE,
        "Diary report exceeds the size limit",
    )
}

fn write_failure(error: DiaryWriteError) -> Response {
    match error {
        DiaryWriteError::Replay(ReplayConflict::InputChanged) => detail(
            StatusCode::CONFLICT,
            "Idempotency key already used for different data",
        ),
        DiaryWriteError::Replay(ReplayConflict::AuditUnavailable) => detail(
            StatusCode::CONFLICT,
            "Creation audit unavailable for idempotent replay",
        ),
        DiaryWriteError::TrackingDisabled | DiaryWriteError::OperationForbidden => {
            detail(StatusCode::FORBIDDEN, "Operation not allowed")
        }
        DiaryWriteError::EventMissing => detail(StatusCode::NOT_FOUND, "Record not found"),
        DiaryWriteError::RevisionChanged => {
            detail(StatusCode::CONFLICT, "Event changed; reload before editing")
        }
        DiaryWriteError::Input(_)
        | DiaryWriteError::CustomEntry
        | DiaryWriteError::CustomInput(_)
        | DiaryWriteError::InvalidRelation
        | DiaryWriteError::Replay(ReplayConflict::InvalidKey) => invalid_arguments(),
        _ => storage_unavailable(),
    }
}
