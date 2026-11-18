use axum::http::StatusCode;
use axum::{
    Json,
    body::to_bytes,
    extract::{Path, Request, State},
    http::Method,
    response::{IntoResponse, Response},
};
use ga_db::source_pages::{SourceMappingError, SourceMetricMappingDraft};
use ga_domain::access::Scope;
use serde_json::{Value, json};

use super::{
    AppState,
    diary::{access_failure, authorized},
    method_not_allowed, private_cache,
    tools::{MAX_REQUEST_BYTES, detail},
};
use crate::{configuration, extensions::configured_source_fingerprint};

pub(super) async fn preview(State(state): State<AppState>, request: Request) -> Response {
    handle(&state, request, Operation::Preview).await
}
pub(super) async fn collection(State(state): State<AppState>, request: Request) -> Response {
    handle(&state, request, Operation::Collection).await
}
pub(super) async fn item(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    handle(&state, request, Operation::Delete(id)).await
}

enum Operation {
    Preview,
    Collection,
    Delete(String),
}

async fn handle(state: &AppState, request: Request, operation: Operation) -> Response {
    let (head, body) = request.into_parts();
    let response = async {
        let allowed = match operation {
            Operation::Collection => "GET, POST",
            Operation::Delete(_) => "DELETE",
            Operation::Preview => "POST",
        };
        if !matches!(
            (&operation, &head.method),
            (Operation::Collection, &Method::GET | &Method::POST)
                | (Operation::Delete(_), &Method::DELETE)
                | (Operation::Preview, &Method::POST)
        ) {
            return method_not_allowed(allowed);
        }
        let (database, owner) = match authorized(state, &head.headers, &[Scope::Admin]).await {
            Ok(context) => context,
            Err(status) => return access_failure(status),
        };
        let result = match operation {
            Operation::Delete(id) => database.delete_source_metric_mapping(owner, &id).await,
            Operation::Collection if head.method == Method::GET => {
                let mut limit = 100;
                let mut after = None;
                for (key, value) in
                    form_urlencoded::parse(head.uri.query().unwrap_or_default().as_bytes())
                {
                    match key.as_ref() {
                        "limit" => {
                            let Ok(number) = value.parse::<u16>() else {
                                return invalid();
                            };
                            limit = number;
                        }
                        "after_id" => after = Some(value.into_owned()),
                        _ => {}
                    }
                }
                database
                    .list_source_metric_mappings(owner, limit, after.as_deref())
                    .await
            }
            operation => {
                let Some(value) = to_bytes(body, MAX_REQUEST_BYTES)
                    .await
                    .ok()
                    .and_then(|body| serde_json::from_slice::<Value>(&body).ok())
                else {
                    return invalid();
                };
                let (draft, confirmation, reconfirm) = if matches!(operation, Operation::Preview) {
                    (&value, None, false)
                } else {
                    let Some(object) = value.as_object().filter(|object| {
                        object.keys().all(|key| {
                            matches!(
                                key.as_str(),
                                "draft" | "confirmation_token" | "reconfirm_source_identity"
                            )
                        })
                    }) else {
                        return invalid();
                    };
                    let Some(token) = object.get("confirmation_token").and_then(Value::as_str)
                    else {
                        return invalid();
                    };
                    let Some(reconfirm) = object
                        .get("reconfirm_source_identity")
                        .map_or(Some(false), Value::as_bool)
                    else {
                        return invalid();
                    };
                    (&value["draft"], Some(token), reconfirm)
                };
                let Ok(draft) = SourceMetricMappingDraft::from_json(draft) else {
                    return invalid();
                };
                let Some(hash) = source_hash(draft.instance()) else {
                    return invalid();
                };
                if let Some(token) = confirmation {
                    database
                        .confirm_source_metric_mapping(owner, &draft, &hash, token, reconfirm)
                        .await
                } else {
                    database
                        .preview_source_metric_mapping(owner, &draft, &hash)
                        .await
                }
            }
        };
        result.map_or_else(failure, |document| Json(document).into_response())
    }
    .await;
    private_cache(response, &head.headers)
}

fn source_hash(id: &str) -> Option<String> {
    let [configured, directory] = configuration::read(["GA_INTEGRATIONS", "GA_TOKEN_DIR"]).ok()?;
    let configured = configured.or_else(|| {
        let directory = directory.as_deref().unwrap_or("tokens/garmin");
        (id == "source:garmin:primary"
            && std::path::Path::new(directory)
                .join("garmin_tokens.json")
                .is_file())
        .then(|| json!([{"id":id,"kind":"source","provider":"garmin"}]).to_string())
    });
    configured_source_fingerprint(configured.as_deref(), id)
}

fn invalid() -> Response {
    detail(StatusCode::UNPROCESSABLE_ENTITY, "Invalid arguments")
}
fn failure(error: SourceMappingError) -> Response {
    let status = match error {
        SourceMappingError::Invalid => StatusCode::UNPROCESSABLE_ENTITY,
        SourceMappingError::Missing => StatusCode::NOT_FOUND,
        SourceMappingError::Source(_) => return access_failure(StatusCode::SERVICE_UNAVAILABLE),
        _ => StatusCode::CONFLICT,
    };
    (status, Json(json!({"detail":error.to_string()}))).into_response()
}
