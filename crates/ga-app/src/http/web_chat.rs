use axum::{
    Json,
    body::{Body, Bytes, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use futures_util::stream;
use ga_app::{
    model_gateway::RuntimeModelSettings,
    urgent_screen::{obvious_urgent_symptoms, urgent_notice},
};
use ga_db::{
    Database,
    web_chat::{WebChatError, WebChatModelPreparation, WebChatSnapshot, WebChatSubmission},
};
use ga_domain::{access::Scope, model_consent::ModelCategories};
use serde::Deserialize;
use serde_json::json;
use uuid::Uuid;

use super::{AppState, method_not_allowed, private_cache, tools::MAX_REQUEST_BYTES};

pub(super) async fn messages(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = if !matches!(parts.method, Method::GET | Method::POST) {
        method_not_allowed("POST")
    } else {
        match owner_database(&state, &parts.headers) {
            Ok(database) => {
                let result = if parts.method == Method::POST {
                    submit(&state, database, body).await
                } else {
                    database.web_chat_messages().await
                };
                result.map(stream_snapshot).unwrap_or_else(failure)
            }
            Err((status, detail)) => (status, Json(json!({"detail":detail}))).into_response(),
        }
    };
    private_cache(response, &parts.headers)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Message {
    client_message_id: String,
    text: String,
}

async fn submit(
    state: &AppState,
    database: &Database,
    body: Body,
) -> Result<WebChatSnapshot, WebChatError> {
    let bytes = to_bytes(body, MAX_REQUEST_BYTES)
        .await
        .map_err(|_| WebChatError::Invalid)?;
    let message: Message = serde_json::from_slice(&bytes).map_err(|_| WebChatError::Invalid)?;
    let client_message_id =
        Uuid::parse_str(&message.client_message_id).map_err(|_| WebChatError::Invalid)?;
    if !(1..=16_000).contains(&message.text.chars().count()) {
        return Err(WebChatError::Invalid);
    }
    let urgent =
        obvious_urgent_symptoms(message.text.trim()).map_err(|_| WebChatError::Database)?;
    let request = WebChatSubmission {
        client_message_id,
        text: &message.text,
        configured_timezone: &state.configured_timezone,
        configured_locale: &state.configured_locale,
        urgent_messages: urgent.then(|| (urgent_notice("ru"), urgent_notice("en"))),
    };
    let text = message.text.trim();
    if urgent
        || matches!(
            text,
            "/help" | "/history" | "/pause" | "/resume" | "/cancel"
        )
        || text.starts_with("/note ")
    {
        return database.send_web_chat(request).await;
    }
    let settings = crate::replay_worker::model_settings();
    let configured = settings.as_ref().map(RuntimeModelSettings::configured);
    let Some(configured) = configured else {
        return database.send_web_chat(request).await;
    };
    if !database
        .model_onboarding_allows(ModelCategories::DIARY)
        .await
        .unwrap_or(false)
    {
        return database.send_web_chat(request).await;
    }
    match database.prepare_web_chat_model(&request).await? {
        WebChatModelPreparation::Replied(snapshot) => Ok(snapshot),
        WebChatModelPreparation::Ready(session) => {
            if let Ok(gateway) = configured.gateway(database.clone(), false, session.at) {
                gateway
                    .process_web_chat(*session, request, || configured.current())
                    .await
            } else {
                database.send_web_chat(request).await
            }
        }
    }
}

fn stream_snapshot(snapshot: WebChatSnapshot) -> Response {
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
        WebChatError::Unavailable | WebChatError::ReusedMessage => StatusCode::CONFLICT,
        WebChatError::Invalid => StatusCode::UNPROCESSABLE_ENTITY,
        WebChatError::Database | WebChatError::Storage(_) => StatusCode::SERVICE_UNAVAILABLE,
    };
    (status, Json(json!({"detail":error.to_string()}))).into_response()
}
