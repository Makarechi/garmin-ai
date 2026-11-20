mod integrations;

use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode},
    response::{IntoResponse, Response},
};
use ga_db::{
    Database,
    onboarding::{OnboardingError, OnboardingPlan},
};
use ga_domain::access::Scope;
use serde_json::json;
use uuid::Uuid;

use super::{
    AppState,
    diary::{access_failure, authorized},
    method_not_allowed, private_cache, request_json,
    tools::{MAX_REQUEST_BYTES, detail, invalid_arguments, storage_unavailable},
};

pub(super) async fn preferences(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = if matches!(parts.method, Method::GET | Method::PUT) {
        let required = &[Scope::ManageDefinitions, Scope::ManageIntegrations];
        match authorized(&state, &parts.headers, required).await {
            Ok((database, owner)) if parts.method == Method::PUT => {
                apply(database, owner, &parts.headers, body).await
            }
            Ok((database, owner)) => match integrations::declared() {
                Some(declared) => match database.onboarding_status(owner, declared).await {
                    Ok(status) => Json(status).into_response(),
                    Err(error) => failure(error),
                },
                None => storage_unavailable(),
            },
            Err(status) => access_failure(status),
        }
    } else {
        method_not_allowed("GET")
    };
    private_cache(response, &parts.headers)
}

pub(super) async fn tracker_profile(
    State(state): State<AppState>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    let response = if method == Method::GET {
        match authorized(&state, &headers, &[Scope::ManageDefinitions]).await {
            Ok((database, owner)) => match database.tracker_profile(owner).await {
                Ok(profile) => Json(json!({"locale":profile.locale,"timezone":profile.timezone}))
                    .into_response(),
                Err(error) => failure(error),
            },
            Err(status) => access_failure(status),
        }
    } else {
        method_not_allowed("GET")
    };
    private_cache(response, &headers)
}

async fn apply(database: &Database, owner: Uuid, headers: &HeaderMap, body: Body) -> Response {
    let data = match to_bytes(body, MAX_REQUEST_BYTES).await {
        Ok(data) => data,
        Err(_) => {
            return detail(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Setup request exceeds the size limit",
            );
        }
    };
    let value = match request_json::parse(&data, headers) {
        Ok(value) => value,
        Err(error) => return error.into_response(),
    };
    if !value.is_object() {
        let issue = if value.is_null() {
            json!({"type":"missing","loc":["body"],"msg":"Field required","input":null})
        } else {
            json!({
                "type":"model_attributes_type","loc":["body"],
                "msg":"Input should be a valid dictionary or object to extract fields from",
                "input":value
            })
        };
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail":[issue]})),
        )
            .into_response();
    }
    let plan = match OnboardingPlan::parse(value.clone()) {
        Ok(plan) => plan,
        Err(OnboardingError::Validation(reason)) if reason != "Invalid arguments" => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail":[{
                    "type":"value_error","loc":["body"],"msg":format!("Value error, {reason}"),
                    "input":value,"ctx":{"error":{}}
                }]})),
            )
                .into_response();
        }
        Err(error) => return failure(error),
    };
    match database.apply_onboarding(owner, plan).await {
        Ok(result) => Json(result).into_response(),
        Err(error) => failure(error),
    }
}

fn failure(error: OnboardingError) -> Response {
    match error {
        OnboardingError::Validation(_)
        | OnboardingError::Tracker(ga_db::diary::actions::TrackerSetupStoreError::Registry(
            ga_db::diary::CustomRegistryError::KeyTaken,
        )) => invalid_arguments(),
        _ => storage_unavailable(),
    }
}
