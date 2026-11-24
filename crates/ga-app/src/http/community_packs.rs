use axum::{
    Json,
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::diary::{
    ManageCustomDefinition,
    actions::{CommunityPackError, community_pack_catalog, parse_community_pack},
};
use ga_domain::access::Scope;
use serde_json::{Value, json};

use super::{
    AppState,
    diary::{access_failure, authorized},
    method_not_allowed, private_cache,
    tools::detail,
};

pub(super) async fn catalog(State(state): State<AppState>, request: Request) -> Response {
    let headers = request.headers();
    let response = if request.method() != Method::GET {
        method_not_allowed("GET")
    } else if let Some(scopes) = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
    {
        if scopes.permits(&[Scope::ManageDefinitions]) {
            community_pack_catalog().map_or_else(failure, |packs| {
                Json(json!({"packs":packs})).into_response()
            })
        } else {
            access_failure(StatusCode::FORBIDDEN)
        }
    } else {
        access_failure(StatusCode::UNAUTHORIZED)
    };
    private_cache(response, headers)
}

pub(super) async fn preview(State(state): State<AppState>, request: Request) -> Response {
    apply(&state, request, false).await
}

pub(super) async fn import(State(state): State<AppState>, request: Request) -> Response {
    apply(&state, request, true).await
}

async fn apply(state: &AppState, request: Request, confirm: bool) -> Response {
    let (head, body) = request.into_parts();
    let response = async {
        if head.method != Method::POST {
            return method_not_allowed("POST");
        }
        let limit = if confirm { 64_250 } else { 64_000 };
        if let Some(declared) = head
            .headers
            .get(header::CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok())
            .filter(|value| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()))
        {
            let declared = declared.trim_start_matches('0');
            let bound = limit.to_string();
            if declared.len() > bound.len()
                || declared.len() == bound.len() && declared > bound.as_str()
            {
                return too_large();
            }
        }
        let Ok(body) = to_bytes(body, limit).await else {
            return too_large();
        };
        let (database, owner_id) =
            match authorized(state, &head.headers, &[Scope::ManageDefinitions]).await {
                Ok(context) => context,
                Err(status) => return access_failure(status),
            };
        let Some(value) = serde_json::from_slice::<Value>(&body).ok() else {
            return invalid();
        };
        let (pack, confirmation) = if confirm {
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
                            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                })
            else {
                return invalid();
            };
            (&value["pack"], Some(token))
        } else {
            (&value, None)
        };
        let pack = match parse_community_pack(pack) {
            Ok(pack) => pack,
            Err(error) => return failure(error),
        };
        let actor = ManageCustomDefinition {
            owner_id,
            authenticated_actor: "api",
            may_manage_definitions: true,
        };
        let result = if let Some(token) = confirmation {
            database.import_community_pack(actor, &pack, token).await
        } else {
            let reveal = state
                .authorization
                .authenticate(head.headers.get(header::AUTHORIZATION))
                .is_some_and(|scopes| scopes.permits(&[Scope::ReadDiary]));
            database.preview_community_pack(actor, &pack, reveal).await
        };
        result.map_or_else(failure, |document| Json(document).into_response())
    }
    .await;
    private_cache(response, &head.headers)
}

fn invalid() -> Response {
    detail(StatusCode::UNPROCESSABLE_ENTITY, "Invalid arguments")
}

fn too_large() -> Response {
    detail(
        StatusCode::PAYLOAD_TOO_LARGE,
        "Pack JSON exceeds the upload limit",
    )
}
fn failure(error: CommunityPackError) -> Response {
    let status = match error {
        CommunityPackError::Invalid => return invalid(),
        CommunityPackError::PreviewChanged | CommunityPackError::Conflict => StatusCode::CONFLICT,
        _ => return access_failure(StatusCode::SERVICE_UNAVAILABLE),
    };
    (status, Json(json!({"detail":error.to_string()}))).into_response()
}
