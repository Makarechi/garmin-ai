use axum::{
    Json,
    body::{Body, Bytes},
    extract::{Path, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use futures_util::stream;
use ga_db::{Database, web_chat::WebChatError};
use ga_domain::access::Scope;
use serde_json::json;
use uuid::Uuid;

use super::{AppState, method_not_allowed, private_cache};

pub(super) async fn messages(
    State(state): State<AppState>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    let response = if method != Method::GET {
        method_not_allowed("GET")
    } else {
        match owner_database(&state, &headers) {
            Ok(database) => match database.web_chat_messages().await {
                Ok(snapshot) => {
                    let chunks = stream::try_unfold(snapshot, |mut snapshot| async move {
                        Ok::<_, WebChatError>(
                            snapshot
                                .next_json()
                                .await?
                                .map(|bytes| (Bytes::from(bytes), snapshot)),
                        )
                    });
                    (
                        [(header::CONTENT_TYPE, "application/json")],
                        Body::from_stream(chunks),
                    )
                        .into_response()
                }
                Err(error) => failure(error),
            },
            Err((status, detail)) => (status, Json(json!({"detail":detail}))).into_response(),
        }
    };
    private_cache(response, &headers)
}

pub(super) async fn mark_read(
    State(state): State<AppState>,
    Path(id): Path<String>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    let response = if method != Method::POST {
        method_not_allowed("POST")
    } else {
        match (owner_database(&state, &headers), Uuid::parse_str(&id)) {
            (Ok(database), Ok(id)) => match database.mark_web_chat_read(id).await {
                Ok(()) => Json(json!({"status":"read"})).into_response(),
                Err(error) => failure(error),
            },
            (Ok(_), Err(error)) => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail":[{
                    "type":"uuid_parsing","loc":["path","outbox_id"],"input":id,
                    "msg":format!("Input should be a valid UUID, {error}"),"ctx":{"error":error.to_string()},
                }]})),
            ).into_response(),
            (Err((status, detail)), _) => (status, Json(json!({"detail":detail}))).into_response(),
        }
    };
    private_cache(response, &headers)
}

fn owner_database<'a>(
    state: &'a AppState,
    headers: &HeaderMap,
) -> Result<&'a Database, (StatusCode, &'static str)> {
    let scopes = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
        .ok_or((StatusCode::UNAUTHORIZED, "Authentication required"))?;
    if !scopes.permits(&[Scope::Admin]) {
        return Err((StatusCode::FORBIDDEN, "Owner token required"));
    }
    state.database.as_ref().ok_or((
        StatusCode::SERVICE_UNAVAILABLE,
        "Database unavailable or identity is not ready",
    ))
}

fn failure(error: WebChatError) -> Response {
    let status = match error {
        WebChatError::Missing => StatusCode::NOT_FOUND,
        WebChatError::Unavailable => StatusCode::CONFLICT,
        WebChatError::Database | WebChatError::Storage(_) => StatusCode::SERVICE_UNAVAILABLE,
    };
    (status, Json(json!({"detail":error.to_string()}))).into_response()
}
