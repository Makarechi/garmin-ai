use axum::{
    Json,
    extract::State,
    http::{HeaderMap, Method, header},
    response::{IntoResponse, Response},
};
use ga_domain::access::Scope;

use super::{
    AppState, method_not_allowed, private_cache,
    tools::{forbidden, storage_unavailable, unauthorized},
};

pub(super) async fn snapshot(
    State(state): State<AppState>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    respond(state, method, headers, false).await
}

pub(super) async fn metrics(
    State(state): State<AppState>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    respond(state, method, headers, true).await
}

async fn respond(state: AppState, method: Method, headers: HeaderMap, metrics: bool) -> Response {
    let response = if method != Method::GET {
        method_not_allowed("GET")
    } else if let Some(scopes) = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
    {
        if !scopes.permits(&[Scope::Admin]) {
            forbidden()
        } else if let Some(database) = state.database.as_ref() {
            match database.operations_snapshot().await {
                Some(snapshot) if metrics => snapshot.prometheus().into_response(),
                Some(snapshot) => Json(snapshot).into_response(),
                None => storage_unavailable(),
            }
        } else {
            storage_unavailable()
        }
    } else {
        unauthorized()
    };
    private_cache(response, &headers)
}
