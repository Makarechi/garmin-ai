use axum::{
    Json,
    body::to_bytes,
    extract::{Path, Request, State},
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use ga_db::health_analysis::HypothesisError;
use ga_domain::{access::Scope, hypothesis::HypothesisSpec, timestamp::StoredInstant};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    AppState, diary, method_not_allowed, private_cache,
    tools::{MAX_REQUEST_BYTES, detail, health_failure, invalid_arguments, storage_unavailable},
};

fn now() -> Option<StoredInstant> {
    let micros = OffsetDateTime::now_utc()
        .unix_timestamp_nanos()
        .div_euclid(1000);
    StoredInstant::from_unix_microseconds(i64::try_from(micros).ok()?).ok()
}

pub(super) async fn collection(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = if parts.method != Method::POST {
        method_not_allowed("POST")
    } else {
        match diary::authorized(
            &state,
            &parts.headers,
            &[Scope::ReadHealth, Scope::ReadDiary, Scope::WriteDiary],
        )
        .await
        {
            Err(status) => diary::access_failure(status),
            Ok((database, owner)) => {
                let result = async {
                    let bytes = to_bytes(body, MAX_REQUEST_BYTES)
                        .await
                        .map_err(|_| HypothesisError::Spec)?;
                    let bytes = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
                    let value = serde_json::from_slice(bytes).map_err(|_| HypothesisError::Spec)?;
                    let spec =
                        HypothesisSpec::from_json(&value).map_err(|_| HypothesisError::Spec)?;
                    database
                        .register_hypothesis(
                            owner,
                            &spec,
                            now().ok_or(HypothesisError::Calendar)?,
                            &state.configured_timezone,
                        )
                        .await
                }
                .await;
                result_response(result)
            }
        }
    };
    private_cache(response, &parts.headers)
}

pub(super) async fn item(
    State(state): State<AppState>,
    Path(identity): Path<String>,
    request: Request,
) -> Response {
    let response = if request.method() != Method::GET {
        method_not_allowed("GET")
    } else {
        match diary::authorized(
            &state,
            request.headers(),
            &[Scope::ReadHealth, Scope::ReadDiary],
        )
        .await
        {
            Err(status) => diary::access_failure(status),
            Ok((database, _)) => match Uuid::parse_str(&identity) {
                Ok(identity) => result_response(database.hypothesis(identity).await),
                Err(_) => invalid_arguments(),
            },
        }
    };
    private_cache(response, request.headers())
}

pub(super) async fn recheck(
    State(state): State<AppState>,
    Path(identity): Path<String>,
    request: Request,
) -> Response {
    mutate(state, identity, request, true).await
}

pub(super) async fn stop(
    State(state): State<AppState>,
    Path(identity): Path<String>,
    request: Request,
) -> Response {
    mutate(state, identity, request, false).await
}

async fn mutate(state: AppState, identity: String, request: Request, recheck: bool) -> Response {
    let response = if request.method() != Method::POST {
        method_not_allowed("POST")
    } else {
        match diary::authorized(
            &state,
            request.headers(),
            &[Scope::ReadHealth, Scope::ReadDiary, Scope::WriteDiary],
        )
        .await
        {
            Err(status) => diary::access_failure(status),
            Ok((database, owner)) => match (Uuid::parse_str(&identity), now()) {
                (Ok(identity), Some(now)) => {
                    let result = if recheck {
                        database
                            .recheck_hypothesis(owner, identity, now, &state.configured_timezone)
                            .await
                    } else {
                        database.stop_hypothesis(owner, identity, now).await
                    };
                    result_response(result)
                }
                (Err(_), _) => invalid_arguments(),
                _ => storage_unavailable(),
            },
        }
    };
    private_cache(response, request.headers())
}

fn result_response(result: Result<serde_json::Value, HypothesisError>) -> Response {
    match result {
        Ok(value) => Json(value).into_response(),
        Err(HypothesisError::Missing) => detail(StatusCode::NOT_FOUND, "Record not found"),
        Err(HypothesisError::Conflict(message)) => detail(StatusCode::CONFLICT, message),
        Err(HypothesisError::Value(_) | HypothesisError::Spec) => invalid_arguments(),
        Err(HypothesisError::Health(error)) => health_failure(error),
        Err(HypothesisError::Write(error)) => diary::write_failure(error),
        Err(HypothesisError::Calendar) => {
            (StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error").into_response()
        }
        Err(HypothesisError::Database) => storage_unavailable(),
    }
}
