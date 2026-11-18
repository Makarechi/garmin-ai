use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::{
    Database,
    diary::{CreateSystemEvent, DiaryWriteError, UpdateSystemEvent, event_document},
};
use ga_domain::{
    access::Scope,
    event_input::{EventInput, ValidationMode},
    event_replay::{ReplayConflict, check_idempotency_key},
};
use uuid::Uuid;

use super::{
    AppState, method_not_allowed, private_cache,
    tools::{
        MAX_REQUEST_BYTES, detail, forbidden, integer_input, invalid_arguments,
        storage_unavailable, unauthorized,
    },
};

pub(super) async fn create(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = match parts.method {
        Method::POST => {
            match authorized(
                &state,
                &parts.headers,
                &[Scope::ReadDiary, Scope::WriteDiary],
            )
            .await
            {
                Ok((database, owner)) => create_entry(database, owner, &parts.headers, body).await,
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
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let required: &[Scope] = match parts.method {
        Method::GET => &[Scope::ReadDiary],
        Method::PUT => &[Scope::ReadDiary, Scope::WriteDiary],
        _ => return private_cache(method_not_allowed("GET, PUT"), &parts.headers),
    };
    let response = match authorized(&state, &parts.headers, required).await {
        Ok((database, owner)) => match id.parse() {
            Ok(id) if parts.method == Method::PUT => update_entry(database, owner, id, body).await,
            Ok(id) => match database.owner_event(owner, id).await {
                Ok(Some(value)) => Json(value).into_response(),
                Ok(None) => record_missing(),
                Err(_) => storage_unavailable(),
            },
            Err(_) => invalid_arguments(),
        },
        Err(status) => access_failure(status),
    };
    private_cache(response, &parts.headers)
}

async fn update_entry(database: &Database, owner: Uuid, id: Uuid, body: Body) -> Response {
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
    let Some(event) = body.remove("event") else {
        return invalid_arguments();
    };
    let Ok(input) = serde_json::to_vec(&event) else {
        return invalid_arguments();
    };
    let Ok(report) = EventInput::from_json(&input, MAX_REQUEST_BYTES, ValidationMode::NewReport)
    else {
        return invalid_arguments();
    };
    let result = database
        .update_system_event(UpdateSystemEvent {
            owner_id: owner,
            authenticated_actor: "api",
            event_id: id,
            expected_revision: i32::try_from(revision).unwrap_or(0),
            report: &report,
            operation_id: None,
        })
        .await;
    match result {
        Ok(entry) => event_response(entry.snapshot),
        Err(error) => write_failure(error),
    }
}

async fn create_entry(
    database: &Database,
    owner: Uuid,
    headers: &HeaderMap,
    body: Body,
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
    let Ok(report) = EventInput::from_json(body, MAX_REQUEST_BYTES, ValidationMode::NewReport)
    else {
        return invalid_arguments();
    };
    let written = database
        .create_system_event(CreateSystemEvent {
            owner_id: owner,
            authenticated_actor: "api",
            report: &report,
            idempotency_key: idempotency.as_deref(),
            operation_id: None,
            clock_uncertainty_seconds: None,
        })
        .await;
    match written {
        Ok(entry) => event_response(entry.snapshot),
        Err(error) => write_failure(error),
    }
}

fn record_missing() -> Response {
    detail(StatusCode::NOT_FOUND, "Record not found")
}

fn report_too_large() -> Response {
    detail(
        StatusCode::PAYLOAD_TOO_LARGE,
        "Diary report exceeds the size limit",
    )
}

fn event_response(snapshot: serde_json::Value) -> Response {
    match event_document(snapshot) {
        Ok(value) => Json(value).into_response(),
        Err(_) => storage_unavailable(),
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
        DiaryWriteError::TrackingDisabled => detail(StatusCode::FORBIDDEN, "Operation not allowed"),
        DiaryWriteError::EventMissing => record_missing(),
        DiaryWriteError::RevisionChanged => {
            detail(StatusCode::CONFLICT, "Event changed; reload before editing")
        }
        DiaryWriteError::SelfReference(message) => detail(StatusCode::CONFLICT, message),
        DiaryWriteError::LinkedObservations => detail(
            StatusCode::CONFLICT,
            "Detach linked medication or symptom observations before removing or changing this migraine",
        ),
        DiaryWriteError::SymptomBounds => detail(
            StatusCode::CONFLICT,
            "Episode bounds would strand linked symptom observations",
        ),
        DiaryWriteError::Input(_)
        | DiaryWriteError::CustomEntry
        | DiaryWriteError::InvalidRelation
        | DiaryWriteError::Replay(ReplayConflict::InvalidKey) => invalid_arguments(),
        _ => storage_unavailable(),
    }
}
