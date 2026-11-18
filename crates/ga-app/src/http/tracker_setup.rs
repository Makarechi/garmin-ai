use axum::{
    Json,
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::diary::{
    CustomRegistryError, ManageCustomDefinition,
    actions::{TrackerActionError, TrackerSetupStoreError},
};
use ga_domain::{access::Scope, event_schema::TrackerSetupDraft};
use serde_json::Value;
use uuid::Uuid;

use super::{
    AppState,
    diary::{access_failure, authorized},
    method_not_allowed, private_cache,
    tools::{MAX_REQUEST_BYTES, detail},
};

pub(super) async fn preview(State(state): State<AppState>, request: Request) -> Response {
    tracker_setup(&state, request, false).await
}

pub(super) async fn confirm(State(state): State<AppState>, request: Request) -> Response {
    tracker_setup(&state, request, true).await
}

pub(super) async fn export_pack(State(state): State<AppState>, request: Request) -> Response {
    let (head, body) = request.into_parts();
    let response = async {
        if head.method != Method::POST {
            return method_not_allowed("POST");
        }
        let (database, owner_id) =
            match authorized(&state, &head.headers, &[Scope::ManageDefinitions]).await {
                Ok(context) => context,
                Err(status) => return access_failure(status),
            };
        let Some(value) = to_bytes(body, MAX_REQUEST_BYTES)
            .await
            .ok()
            .and_then(|body| serde_json::from_slice::<Value>(&body).ok())
        else {
            return invalid();
        };
        let Some(object) = value.as_object().filter(|object| object.len() == 1) else {
            return invalid();
        };
        let Some(ids) = object
            .get("definition_ids")
            .and_then(Value::as_array)
            .filter(|ids| (1..=32).contains(&ids.len()))
        else {
            return invalid();
        };
        let Some(ids) = ids
            .iter()
            .map(|id| id.as_str().and_then(|id| id.parse::<Uuid>().ok()))
            .collect::<Option<Vec<_>>>()
        else {
            return invalid();
        };
        match database.export_tracker_pack(owner_id, &ids).await {
            Ok(document) => Json(document).into_response(),
            Err(TrackerActionError::Missing) => detail(StatusCode::NOT_FOUND, "Record not found"),
            Err(TrackerActionError::InvalidContract) => invalid(),
            Err(_) => access_failure(StatusCode::SERVICE_UNAVAILABLE),
        }
    }
    .await;
    private_cache(response, &head.headers)
}

async fn tracker_setup(state: &AppState, request: Request, confirm: bool) -> Response {
    let (head, body) = request.into_parts();
    let response = async {
        if head.method != Method::POST {
            return method_not_allowed("POST");
        }
        let (database, owner_id) =
            match authorized(state, &head.headers, &[Scope::ManageDefinitions]).await {
                Ok(context) => context,
                Err(status) => return access_failure(status),
            };
        let Some(value) = to_bytes(body, MAX_REQUEST_BYTES)
            .await
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        else {
            return invalid();
        };
        let (draft, token) = if confirm {
            let Some(object) = value.as_object().filter(|object| object.len() == 2) else {
                return invalid();
            };
            let Some(token) = object
                .get("confirmation_token")
                .and_then(Value::as_str)
                .filter(|token| {
                    token.len() == 64
                        && token
                            .bytes()
                            .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
                })
            else {
                return invalid();
            };
            (&value["draft"], Some(token))
        } else {
            (&value, None)
        };
        let Ok(draft) = TrackerSetupDraft::from_json(draft) else {
            return invalid();
        };
        let actor = ManageCustomDefinition {
            owner_id,
            authenticated_actor: "api",
            may_manage_definitions: true,
        };
        let result = if let Some(token) = token {
            let settings = draft.document();
            if (settings["reminder_enabled"] == true || !settings["reminder_time"].is_null())
                && !state
                    .authorization
                    .authenticate(head.headers.get(header::AUTHORIZATION))
                    .is_some_and(|scopes| scopes.permits(&[Scope::ManageIntegrations]))
            {
                return detail(
                    StatusCode::FORBIDDEN,
                    "Reminder setup requires integration management",
                );
            }
            database.confirm_tracker_setup(actor, &draft, token).await
        } else {
            database.preview_tracker_setup(actor, &draft).await
        };
        result.map_or_else(failure, |document| Json(document).into_response())
    }
    .await;
    private_cache(response, &head.headers)
}

fn invalid() -> Response {
    detail(StatusCode::UNPROCESSABLE_ENTITY, "Invalid arguments")
}

fn failure(error: TrackerSetupStoreError) -> Response {
    match error {
        TrackerSetupStoreError::PreviewChanged => detail(
            StatusCode::CONFLICT,
            "Tracker preview changed; preview it again",
        ),
        TrackerSetupStoreError::Registry(CustomRegistryError::KeyTaken) => {
            detail(StatusCode::CONFLICT, "Definition key already exists")
        }
        TrackerSetupStoreError::Registry(CustomRegistryError::Forbidden) => {
            access_failure(StatusCode::FORBIDDEN)
        }
        TrackerSetupStoreError::Registry(CustomRegistryError::Spec(_)) => invalid(),
        _ => access_failure(StatusCode::SERVICE_UNAVAILABLE),
    }
}
