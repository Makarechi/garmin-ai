use std::{future::Future, time::Duration};

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
        CreateSystemEvent, DeleteEvent, DiaryWriteError, UpdateSystemEvent,
        custom_entries::{CreateCustomEntry, UpdateCustomEntry},
        event_document,
    },
};
use ga_domain::{
    access::Scope,
    event_input::{EventInput, InvalidEvent, ValidationMode},
    event_replay::{ReplayConflict, check_idempotency_key},
    event_schema::CustomEntry,
};
use tokio::sync::{Mutex, Semaphore, SemaphorePermit};
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

const DIARY_WRITE_CAPACITY: usize = 16;
const DIARY_QUEUE_BODY_BYTES: usize = 2 * MAX_REQUEST_BYTES;
const DIARY_LOCK_RETRY: Duration = Duration::from_millis(50);

pub(super) struct WriteQueue {
    slots: Semaphore,
    body_bytes: Semaphore,
    active: Mutex<()>,
}

impl WriteQueue {
    pub(super) fn new() -> Self {
        Self {
            slots: Semaphore::new(DIARY_WRITE_CAPACITY),
            // Bound request data retained while writes wait for the database lock.
            body_bytes: Semaphore::new(DIARY_QUEUE_BODY_BYTES),
            active: Mutex::new(()),
        }
    }

    pub(super) fn close(&self) {
        self.slots.close();
        self.body_bytes.close();
    }

    fn reserve_body(&self, bytes: usize) -> Option<SemaphorePermit<'_>> {
        self.body_bytes
            .try_acquire_many(bytes.try_into().ok()?)
            .ok()
    }

    async fn submit<T, F>(&self, mut write: impl FnMut() -> F + Send) -> Result<T, DiaryWriteError>
    where
        F: Future<Output = Result<T, DiaryWriteError>> + Send,
    {
        let _slot = self
            .slots
            .try_acquire()
            .map_err(|_| DiaryWriteError::Busy)?;
        let _active = self.active.lock().await;
        while !self.slots.is_closed() {
            match write().await {
                // The database rolls back the attempt before reporting lock contention.
                Err(DiaryWriteError::Busy) => tokio::time::sleep(DIARY_LOCK_RETRY).await,
                result => return result,
            }
        }
        Err(DiaryWriteError::Busy)
    }
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
                Ok(id) => {
                    update_tracker_entry(&state.diary_writes, database, owner, id, body).await
                }
                Err(_) => invalid_arguments(),
            },
            Err(status) => access_failure(status),
        }
    } else {
        method_not_allowed("PUT")
    };
    private_cache(response, &parts.headers)
}

async fn update_tracker_entry(
    writes: &WriteQueue,
    database: &Database,
    owner: Uuid,
    id: Uuid,
    body: Body,
) -> Response {
    let Ok(bytes) = to_bytes(body, MAX_REQUEST_BYTES).await else {
        return report_too_large();
    };
    let Some(_body_permit) = writes.reserve_body(bytes.len()) else {
        return write_failure(DiaryWriteError::Busy);
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
    match writes
        .submit(|| {
            database.update_custom_entry_document(UpdateCustomEntry {
                owner_id: owner,
                actor: "api",
                id,
                expected_revision: i32::try_from(revision).unwrap_or(0),
                entry: &entry,
                evidence_refs: &[],
            })
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
                    create_entry(
                        &state.diary_writes,
                        database,
                        owner,
                        &parts.headers,
                        body,
                        kind,
                    )
                    .await
                }
                Err(status) => access_failure(status),
            }
        }
        _ => method_not_allowed("POST"),
    };
    private_cache(response, &parts.headers)
}

pub(super) async fn undo(
    State(state): State<AppState>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    let response = if method == Method::POST {
        match authorized(&state, &headers, &[Scope::ReadDiary, Scope::WriteDiary]).await {
            Ok((database, owner)) => match idempotency_key(&headers) {
                Ok(key) => match state
                    .diary_writes
                    .submit(|| {
                        database.undo_api_diary(owner, key.as_deref(), &state.configured_timezone)
                    })
                    .await
                {
                    Ok(receipt) => Json(receipt).into_response(),
                    Err(error) => write_failure(error),
                },
                Err(_) => invalid_arguments(),
            },
            Err(status) => access_failure(status),
        }
    } else {
        method_not_allowed("POST")
    };
    private_cache(response, &headers)
}

pub(super) async fn entry(
    Path(id): Path<String>,
    State(state): State<AppState>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let required: &[Scope] = match parts.method {
        Method::GET => &[Scope::ReadDiary],
        Method::PUT | Method::DELETE => &[Scope::ReadDiary, Scope::WriteDiary],
        _ => return private_cache(method_not_allowed("GET, PUT, DELETE"), &parts.headers),
    };
    let response = match authorized(&state, &parts.headers, required).await {
        Ok((database, owner)) => match id.parse() {
            Ok(id) if parts.method == Method::PUT => {
                update_entry(&state.diary_writes, database, owner, id, body).await
            }
            Ok(id) if parts.method == Method::DELETE => {
                delete_entry(&state.diary_writes, database, owner, id, parts.uri.query()).await
            }
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

async fn update_entry(
    writes: &WriteQueue,
    database: &Database,
    owner: Uuid,
    id: Uuid,
    body: Body,
) -> Response {
    let Ok(bytes) = to_bytes(body, MAX_REQUEST_BYTES).await else {
        return report_too_large();
    };
    let Some(_body_permit) = writes.reserve_body(bytes.len()) else {
        return write_failure(DiaryWriteError::Busy);
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
    let report = match EventInput::from_json(&input, MAX_REQUEST_BYTES, ValidationMode::NewReport) {
        Ok(report) => report,
        Err(error) => return system_event_rejection(error, &input, &["body", "event"]),
    };
    let result = writes
        .submit(|| {
            database.update_system_event(UpdateSystemEvent {
                owner_id: owner,
                authenticated_actor: "api",
                event_id: id,
                expected_revision: i32::try_from(revision).unwrap_or(0),
                report: &report,
                operation_id: None,
            })
        })
        .await;
    match result {
        Ok(entry) => event_response(entry.snapshot),
        Err(error) => write_failure(error),
    }
}

async fn delete_entry(
    writes: &WriteQueue,
    database: &Database,
    owner: Uuid,
    id: Uuid,
    query: Option<&str>,
) -> Response {
    let expected = query.and_then(|raw| {
        form_urlencoded::parse(raw.as_bytes())
            .filter_map(|(key, value)| (key == "revision").then_some(value))
            .last()?
            .trim()
            .parse::<i32>()
            .ok()
            .filter(|revision| *revision >= 1)
    });
    let Some(expected_revision) = expected else {
        return invalid_arguments();
    };
    match writes
        .submit(|| {
            database.delete_event(DeleteEvent {
                owner_id: owner,
                authenticated_actor: "api",
                event_id: id,
                expected_revision,
            })
        })
        .await
    {
        Ok(deleted) if deleted.snapshot.get("kind").is_none() => {
            Json(deleted.snapshot).into_response()
        }
        Ok(deleted) => event_response(deleted.snapshot),
        Err(DiaryWriteError::EventMissing) => record_missing(),
        Err(DiaryWriteError::RevisionChanged) => detail(
            StatusCode::CONFLICT,
            "Event changed; reload before deleting",
        ),
        Err(DiaryWriteError::DeletionForbidden) => {
            detail(StatusCode::FORBIDDEN, "Operation not allowed")
        }
        Err(DiaryWriteError::LinkedObservations) => detail(
            StatusCode::CONFLICT,
            "Detach linked medication or symptom observations before removing or changing this migraine",
        ),
        Err(error @ DiaryWriteError::Busy) => write_failure(error),
        Err(_) => storage_unavailable(),
    }
}

fn record_missing() -> Response {
    detail(StatusCode::NOT_FOUND, "Record not found")
}

pub(super) fn event_response(snapshot: serde_json::Value) -> Response {
    match event_document(snapshot) {
        Ok(value) => Json(value).into_response(),
        Err(_) => storage_unavailable(),
    }
}

async fn create_entry(
    writes: &WriteQueue,
    database: &Database,
    owner: Uuid,
    headers: &HeaderMap,
    body: Body,
    kind: EntryKind,
) -> Response {
    let Ok(idempotency) = idempotency_key(headers) else {
        return invalid_arguments();
    };
    let Ok(body) = to_bytes(body, MAX_REQUEST_BYTES).await else {
        return report_too_large();
    };
    let Some(_body_permit) = writes.reserve_body(body.len()) else {
        return write_failure(DiaryWriteError::Busy);
    };
    let body = body.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&body);
    let written = match kind {
        EntryKind::System => {
            let report =
                match EventInput::from_json(body, MAX_REQUEST_BYTES, ValidationMode::NewReport) {
                    Ok(report) => report,
                    Err(error) => return system_event_rejection(error, body, &["body"]),
                };
            writes
                .submit(|| {
                    database.create_system_event(CreateSystemEvent {
                        owner_id: owner,
                        authenticated_actor: "api",
                        report: &report,
                        idempotency_key: idempotency.as_deref(),
                        operation_id: None,
                        clock_uncertainty_seconds: None,
                    })
                })
                .await
        }
        EntryKind::Tracker => {
            let Ok(entry) = CustomEntry::from_json(body, MAX_REQUEST_BYTES) else {
                return invalid_arguments();
            };
            writes
                .submit(|| {
                    database.create_custom_entry(CreateCustomEntry {
                        owner_id: owner,
                        actor: "api",
                        entry: &entry,
                        idempotency_key: idempotency.as_deref(),
                        evidence_refs: &[],
                    })
                })
                .await
        }
    };
    match written {
        Ok(entry) => event_response(entry.snapshot),
        Err(error) => write_failure(error),
    }
}

fn system_event_rejection(error: InvalidEvent, input: &[u8], location: &[&str]) -> Response {
    if error != InvalidEvent::CoverageRequired {
        return invalid_arguments();
    }
    let Ok(submitted) = serde_json::from_slice::<serde_json::Value>(input) else {
        return invalid_arguments();
    };
    let message = if submitted["payload"]["type"] == "caffeine_absence" {
        "Value error, Caffeine absence requires an end after its start"
    } else {
        "Value error, Coverage observation requires a nonempty covered interval"
    };
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(serde_json::json!({
            "detail": [{
                "type": "value_error", "loc": location, "msg": message,
                "input": submitted, "ctx": {"error": {}},
            }]
        })),
    )
        .into_response()
}

fn report_too_large() -> Response {
    detail(
        StatusCode::PAYLOAD_TOO_LARGE,
        "Diary report exceeds the size limit",
    )
}

fn idempotency_key(headers: &HeaderMap) -> Result<Option<String>, ReplayConflict> {
    headers
        .get("idempotency-key")
        .map(|key| {
            let key: String = key
                .as_bytes()
                .iter()
                .map(|byte| char::from(*byte))
                .collect();
            check_idempotency_key(&key)?;
            Ok(key)
        })
        .transpose()
}

pub(super) async fn authorized<'a>(
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

pub(super) fn access_failure(status: StatusCode) -> Response {
    match status {
        StatusCode::UNAUTHORIZED => unauthorized(),
        StatusCode::FORBIDDEN => forbidden(),
        _ => storage_unavailable(),
    }
}

pub(super) fn write_failure(error: DiaryWriteError) -> Response {
    match error {
        DiaryWriteError::Busy => (
            [(header::RETRY_AFTER, "1")],
            detail(StatusCode::SERVICE_UNAVAILABLE, "Diary writes are busy"),
        )
            .into_response(),
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
        DiaryWriteError::EventMissing => record_missing(),
        DiaryWriteError::RevisionChanged => {
            detail(StatusCode::CONFLICT, "Event changed; reload before editing")
        }
        DiaryWriteError::SelfReference(message) => detail(StatusCode::CONFLICT, message),
        DiaryWriteError::NothingToUndo => detail(StatusCode::NOT_FOUND, "Record not found"),
        DiaryWriteError::UndoConflict => detail(
            StatusCode::CONFLICT,
            "The last operation has already been changed or undone",
        ),
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
        | DiaryWriteError::CustomInput(_)
        | DiaryWriteError::InvalidRelation
        | DiaryWriteError::Replay(ReplayConflict::InvalidKey) => invalid_arguments(),
        _ => storage_unavailable(),
    }
}
