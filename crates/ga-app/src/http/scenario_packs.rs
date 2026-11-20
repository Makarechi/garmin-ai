use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::{
    Database,
    scenario_packs::{PackSelection, ScenarioPackError},
};
use ga_domain::{access::Scope, event_payload::legacy_boolean};
use serde_json::{Value, json};
use uuid::Uuid;

use super::{
    AppState, method_not_allowed, private_cache,
    tools::{
        MAX_REQUEST_BYTES, detail, forbidden, invalid_arguments, storage_unavailable, unauthorized,
    },
};

const FLAGS: &[&str] = &[
    "tracking_enabled",
    "collection_enabled",
    "reminders_enabled",
    "visible",
    "llm_enabled",
];

pub(super) async fn collection(State(state): State<AppState>, request: Request) -> Response {
    handle(state, None, request).await
}

pub(super) async fn selection(
    State(state): State<AppState>,
    Path(key): Path<String>,
    request: Request,
) -> Response {
    handle(state, Some(&key), request).await
}

async fn handle(state: AppState, key: Option<&str>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let (method, needed): (_, &[_]) = if key.is_some() {
        (
            Method::PUT,
            &[
                Scope::ReadDiary,
                Scope::WriteDiary,
                Scope::ManageIntegrations,
            ],
        )
    } else {
        (Method::GET, &[Scope::ReadDiary])
    };
    let response = if parts.method != method {
        method_not_allowed(if key.is_some() { "PUT" } else { "GET" })
    } else if let Some(scopes) = state
        .authorization
        .authenticate(parts.headers.get(header::AUTHORIZATION))
    {
        if !scopes.permits(needed) {
            forbidden()
        } else if let Some(database) = state.database.as_ref() {
            match (database.enrolled_owner_id().await, key) {
                (Ok(owner), Some(key)) => update(database, owner, key, &parts.headers, body).await,
                (Ok(owner), None) => match database.scenario_packs(owner, true).await {
                    Ok(packs) => Json(json!({"packs": packs})).into_response(),
                    Err(error) => failure(error),
                },
                (Err(_), _) => storage_unavailable(),
            }
        } else {
            storage_unavailable()
        }
    } else {
        unauthorized()
    };
    private_cache(response, &parts.headers)
}

async fn update(
    database: &Database,
    owner: Uuid,
    key: &str,
    headers: &HeaderMap,
    body: Body,
) -> Response {
    let data = match to_bytes(body, MAX_REQUEST_BYTES).await {
        Ok(data) => data,
        Err(_) => {
            return detail(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Scenario pack request exceeds the size limit",
            );
        }
    };
    let value = match super::request_json::parse(&data, headers) {
        Ok(value) => value,
        Err(error) => return error.into_response(),
    };
    let selection = match validate(value) {
        Ok(selection) => selection,
        Err(errors) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail":errors})),
            )
                .into_response();
        }
    };
    match database
        .configure_scenario_pack(owner, true, key, &selection)
        .await
    {
        Ok(pack) => Json(pack).into_response(),
        Err(error) => failure(error),
    }
}

fn validate(value: Value) -> Result<PackSelection, Vec<Value>> {
    let Some(fields) = value.as_object() else {
        return Err(vec![if value.is_null() {
            json!({"type":"missing","loc":["body"],"msg":"Field required","input":null})
        } else {
            json!({"type":"model_attributes_type","loc":["body"],"msg":"Input should be a valid dictionary or object to extract fields from","input":value})
        }]);
    };
    let mut errors = Vec::new();
    let missing =
        |key| json!({"type":"missing","loc":["body",key],"msg":"Field required","input":value});
    let revision = match fields.get("revision") {
        Some(Value::Number(number)) if integer_text(number.as_str()) => {
            if number.as_u64() == Some(0) || number.as_str().starts_with('-') {
                errors.push(json!({"type":"greater_than_equal","loc":["body","revision"],
                    "msg":"Input should be greater than or equal to 1","input":number,"ctx":{"ge":1}}));
            }
            number.as_i64()
        }
        Some(input) => {
            errors.push(json!({"type":"int_type","loc":["body","revision"],"msg":"Input should be a valid integer","input":input}));
            None
        }
        None => {
            errors.push(missing("revision"));
            None
        }
    };
    let mut flags = [false; 5];
    for (flag, &key) in flags.iter_mut().zip(FLAGS) {
        match fields.get(key) {
            Some(input) => match legacy_boolean(input) {
                Some(boolean) => *flag = boolean,
                None => {
                    let (kind, message) = if input.is_string()
                        || input.as_f64().is_some_and(|number| number.fract() == 0.0)
                    {
                        (
                            "bool_parsing",
                            "Input should be a valid boolean, unable to interpret input",
                        )
                    } else {
                        ("bool_type", "Input should be a valid boolean")
                    };
                    errors
                        .push(json!({"type":kind,"loc":["body",key],"msg":message,"input":input}));
                }
            },
            None => errors.push(missing(key)),
        }
    }
    let outcome_goal = match fields.get("outcome_goal") {
        Some(Value::String(goal)) if goal.chars().count() <= 500 => Some(goal.clone()),
        Some(Value::String(goal)) => {
            errors.push(json!({"type":"string_too_long","loc":["body","outcome_goal"],
                "msg":"String should have at most 500 characters","input":goal,"ctx":{"max_length":500}}));
            None
        }
        Some(Value::Null) | None => None,
        Some(input) => {
            errors.push(json!({"type":"string_type","loc":["body","outcome_goal"],"msg":"Input should be a valid string","input":input}));
            None
        }
    };
    for (key, input) in fields.iter().filter(|(key, _)| {
        !matches!(key.as_str(), "revision" | "outcome_goal") && !FLAGS.contains(&key.as_str())
    }) {
        errors.push(json!({"type":"extra_forbidden","loc":["body",key],"msg":"Extra inputs are not permitted","input":input}));
    }
    if !errors.is_empty() {
        return Err(errors);
    }
    let [
        tracking_enabled,
        collection_enabled,
        reminders_enabled,
        visible,
        llm_enabled,
    ] = flags;
    Ok(PackSelection {
        // Revisions beyond the stored range cannot match an existing pack.
        revision: revision.unwrap_or(i64::MAX),
        tracking_enabled,
        collection_enabled,
        reminders_enabled,
        visible,
        llm_enabled,
        outcome_goal,
    })
}

fn integer_text(value: &str) -> bool {
    value
        .strip_prefix('-')
        .unwrap_or(value)
        .bytes()
        .all(|byte| byte.is_ascii_digit())
}

fn failure(error: ScenarioPackError) -> Response {
    match error {
        ScenarioPackError::Forbidden => forbidden(),
        ScenarioPackError::UnknownPack => detail(StatusCode::NOT_FOUND, "Record not found"),
        ScenarioPackError::RevisionChanged => detail(
            StatusCode::CONFLICT,
            "Scenario pack changed; reload before editing",
        ),
        ScenarioPackError::InvalidSelection => invalid_arguments(),
        _ => storage_unavailable(),
    }
}
