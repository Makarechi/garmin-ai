use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use ga_db::{
    Database,
    diary::{CustomRegistryError, ManageCustomDefinition, StoredCustomDefinition},
};
use ga_domain::{access::Scope, event_schema::CustomDefinition};
use serde_json::{Value, json};
use uuid::Uuid;

use super::list;
use crate::http::{
    AppState,
    diary::{access_failure, authorized, write_failure},
    method_not_allowed, private_cache,
    tools::{MAX_REQUEST_BYTES, detail, invalid_arguments, storage_unavailable},
};

#[derive(Clone, Copy)]
enum Operation {
    Draft,
    Revise(Uuid),
    Activate(Uuid),
    Retire(Uuid),
}

pub(in crate::http) async fn collection(
    State(state): State<AppState>,
    request: Request,
) -> Response {
    if request.method() == Method::POST {
        manage(state, request, "POST", Ok(Operation::Draft)).await
    } else {
        list(state, request).await
    }
}

pub(in crate::http) async fn revise(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    manage(state, request, "PUT", id.parse().map(Operation::Revise)).await
}

pub(in crate::http) async fn activate(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    manage(state, request, "POST", id.parse().map(Operation::Activate)).await
}

pub(in crate::http) async fn retire(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    manage(state, request, "POST", id.parse().map(Operation::Retire)).await
}

async fn manage(
    state: AppState,
    request: Request,
    method: &'static str,
    operation: Result<Operation, uuid::Error>,
) -> Response {
    let (parts, body) = request.into_parts();
    let response = if parts.method != method {
        method_not_allowed(method)
    } else {
        match authorized(&state, &parts.headers, &[Scope::ManageDefinitions]).await {
            Ok((database, owner)) => match operation {
                Ok(operation) => store(database, owner, operation, body).await,
                Err(_) => invalid_arguments(),
            },
            Err(status) => access_failure(status),
        }
    };
    private_cache(response, &parts.headers)
}

async fn store(database: &Database, owner: Uuid, operation: Operation, body: Body) -> Response {
    let data = match to_bytes(body, MAX_REQUEST_BYTES).await {
        Ok(data) => data,
        Err(_) => {
            return detail(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Definition request exceeds the size limit",
            );
        }
    };
    let data = data.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&data);
    let Ok(value) = serde_json::from_slice::<Value>(data) else {
        return invalid_arguments();
    };
    let request = ManageCustomDefinition {
        owner_id: owner,
        authenticated_actor: "api",
        may_manage_definitions: true,
    };
    let result = match operation {
        Operation::Draft => {
            let Ok(spec) = CustomDefinition::from_json(&value) else {
                return invalid_arguments();
            };
            database
                .draft_custom_definition(request, &spec)
                .await
                .map(state)
        }
        Operation::Revise(id) => {
            let (Some(revision), Ok(spec)) = (
                revision(&value, true),
                CustomDefinition::from_json(&value["spec"]),
            ) else {
                return invalid_arguments();
            };
            database
                .propose_custom_definition(request, id, revision, &spec)
                .await
                .map(state)
        }
        Operation::Activate(id) => {
            let Some(revision) = revision(&value, false) else {
                return invalid_arguments();
            };
            database
                .activate_custom_definition_document(request, id, revision)
                .await
        }
        Operation::Retire(id) => {
            let Some(revision) = revision(&value, false) else {
                return invalid_arguments();
            };
            database
                .retire_custom_definition(request, id, revision)
                .await
                .map(state)
        }
    };
    match result {
        Ok(value) => Json(value).into_response(),
        Err(error) => failure(error, operation),
    }
}

fn revision(value: &Value, includes_spec: bool) -> Option<i32> {
    let fields = value.as_object()?;
    if fields
        .keys()
        .any(|key| key != "revision" && !(includes_spec && key == "spec"))
    {
        return None;
    }
    let Value::Number(number) = fields.get("revision")? else {
        return None;
    };
    let text = number.as_str();
    if !text.bytes().all(|byte| byte.is_ascii_digit()) || text == "0" {
        return None;
    }
    // Stored revisions are positive. Zero cannot match an out-of-range input.
    Some(text.parse::<i32>().unwrap_or(0))
}

fn state(saved: StoredCustomDefinition) -> Value {
    json!({
        "id":saved.id.to_string(),"key":saved.key,"namespace":"user",
        "status":saved.status,"revision":saved.revision,"current_version":saved.current_version,
    })
}

fn failure(error: CustomRegistryError, operation: Operation) -> Response {
    match error {
        CustomRegistryError::Forbidden => access_failure(StatusCode::FORBIDDEN),
        CustomRegistryError::Missing => detail(StatusCode::NOT_FOUND, "Record not found"),
        CustomRegistryError::RevisionChanged => {
            let message = match operation {
                Operation::Activate(_) => "Definition changed; reload before activation",
                Operation::Retire(_) => "Definition changed; reload before retirement",
                _ => "Definition changed; reload before editing",
            };
            detail(StatusCode::CONFLICT, message)
        }
        CustomRegistryError::KeyTaken
        | CustomRegistryError::KeyChanged
        | CustomRegistryError::Retired
        | CustomRegistryError::NoDraft
        | CustomRegistryError::FieldIdentityChanged
        | CustomRegistryError::Spec(_) => invalid_arguments(),
        CustomRegistryError::Diary(error) => write_failure(error),
        _ => storage_unavailable(),
    }
}
