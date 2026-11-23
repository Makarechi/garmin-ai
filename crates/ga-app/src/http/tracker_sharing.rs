use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::{
    Database,
    share_policy::{DestinationKind, ShareDestination, SharePolicyError, TrackerShareConsent},
};
use ga_domain::access::{Scope, Scopes};
use serde_json::json;

use super::{
    AppState, method_not_allowed, private_cache,
    tools::{
        MAX_REQUEST_BYTES, detail, forbidden, invalid_arguments, storage_unavailable, unauthorized,
    },
};

pub(super) async fn collection(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = match parts.method {
        Method::GET => match authorize(&state, &parts.headers, &[Scope::ManageIntegrations]) {
            Ok((database, scopes)) => match database.list_tracker_shares(scopes).await {
                Ok(consents) => Json(json!({ "consents": consents })).into_response(),
                Err(error) => failure(error),
            },
            Err(error) => error.response(),
        },
        Method::PUT => write(&state, &parts.headers, body, &[Scope::ManageIntegrations]).await,
        _ => method_not_allowed("GET"),
    };
    private_cache(response, &parts.headers)
}

pub(super) async fn create(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = if parts.method == Method::POST {
        write(
            &state,
            &parts.headers,
            body,
            &[Scope::ManageDefinitions, Scope::ManageIntegrations],
        )
        .await
    } else {
        method_not_allowed("POST")
    };
    private_cache(response, &parts.headers)
}

pub(super) async fn revoke(
    State(state): State<AppState>,
    Path((definition, kind, instance)): Path<(String, String, String)>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    let response = if method == Method::DELETE {
        match authorize(&state, &headers, &[Scope::ManageIntegrations]) {
            Ok((database, scopes)) => {
                delete(&database, scopes, &definition, &kind, &instance).await
            }
            Err(error) => error.response(),
        }
    } else {
        method_not_allowed("DELETE")
    };
    private_cache(response, &headers)
}

async fn write(state: &AppState, headers: &HeaderMap, body: Body, required: &[Scope]) -> Response {
    let (database, scopes) = match authorize(state, headers, required) {
        Ok(value) => value,
        Err(error) => return error.response(),
    };
    let Ok(data) = to_bytes(body, MAX_REQUEST_BYTES).await else {
        return detail(
            StatusCode::PAYLOAD_TOO_LARGE,
            "Consent request exceeds the size limit",
        );
    };
    let data = data.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&data);
    let consent = match TrackerShareConsent::parse(data) {
        Ok(consent) => consent,
        Err(error) => return failure(error),
    };
    match database.store_tracker_share(scopes, &consent).await {
        Ok(()) => Json(consent.document()).into_response(),
        Err(error) => failure(error),
    }
}

async fn delete(
    database: &Database,
    scopes: Scopes,
    definition: &str,
    kind: &str,
    instance: &str,
) -> Response {
    let Ok(definition) = definition.parse() else {
        return invalid_arguments();
    };
    let kind = match kind {
        "model" => DestinationKind::Model,
        "channel" => DestinationKind::Channel,
        _ => return invalid_arguments(),
    };
    let Ok(destination) = ShareDestination::new(kind, instance) else {
        return match database.readiness().await {
            Ok(()) => Json(json!({"revoked":false})).into_response(),
            Err(error) => failure(SharePolicyError::Storage(error)),
        };
    };
    match database
        .revoke_tracker_share(scopes, definition, destination)
        .await
    {
        Ok(revoked) => Json(json!({ "revoked": revoked })).into_response(),
        Err(error) => failure(error),
    }
}

#[derive(Clone, Copy)]
enum AccessError {
    Authentication,
    Scopes,
    Storage,
}

impl AccessError {
    fn response(self) -> Response {
        match self {
            Self::Authentication => unauthorized(),
            Self::Scopes => forbidden(),
            Self::Storage => storage_unavailable(),
        }
    }
}

fn authorize(
    state: &AppState,
    headers: &HeaderMap,
    required: &[Scope],
) -> Result<(Database, Scopes), AccessError> {
    let scopes = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
        .ok_or(AccessError::Authentication)?;
    if !scopes.permits(required) {
        return Err(AccessError::Scopes);
    }
    Ok((state.database.clone().ok_or(AccessError::Storage)?, scopes))
}

fn failure(error: SharePolicyError) -> Response {
    match error {
        SharePolicyError::InvalidDestination | SharePolicyError::InvalidConsent => {
            invalid_arguments()
        }
        SharePolicyError::Forbidden => forbidden(),
        SharePolicyError::DefinitionMissing => detail(StatusCode::NOT_FOUND, "Record not found"),
        SharePolicyError::Storage(_) | SharePolicyError::Busy | SharePolicyError::Database => {
            storage_unavailable()
        }
    }
}
