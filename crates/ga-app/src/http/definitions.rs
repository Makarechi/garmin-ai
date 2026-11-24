mod management;
pub(super) use management::{activate, collection, retire, revise};

use std::collections::BTreeMap;

use axum::{
    Json,
    extract::Request,
    http::{HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::diary::DefinitionPage;
use ga_domain::{access::Scope, legacy_json::integer_argument};
use serde_json::{Value, json};

use super::{
    AppState, method_not_allowed, private_cache,
    tools::{document_response, forbidden, storage_unavailable, unauthorized},
};

async fn list(state: AppState, request: Request) -> Response {
    let (parts, _) = request.into_parts();
    let response = if parts.method != Method::GET {
        method_not_allowed("GET")
    } else if let Some(scopes) = state
        .authorization
        .authenticate(parts.headers.get(header::AUTHORIZATION))
    {
        if !scopes.permits(&[Scope::ReadDiary]) && !scopes.permits(&[Scope::ManageDefinitions]) {
            forbidden()
        } else if let Some(database) = state.database.as_ref() {
            let query = parts.uri.query().unwrap_or_default();
            let parameters: BTreeMap<_, _> = form_urlencoded::parse(query.as_bytes()).collect();
            let parameter = |name| parameters.get(name).map(std::convert::AsRef::as_ref);
            let mut errors = Vec::new();
            let before_version = positive(
                parameter("before_version"),
                "before_version",
                None,
                &mut errors,
            );
            let limit = positive(parameter("limit"), "limit", Some(50), &mut errors)
                .and_then(|value| value.parse().ok())
                .unwrap_or(10);
            if errors.is_empty() {
                let page = DefinitionPage {
                    after_key: parameter("after_key"),
                    definition_key: parameter("definition_key"),
                    before_version,
                    limit,
                };
                match database.owner_event_definitions(page).await {
                    Ok((document, cursor)) => {
                        match cursor.as_deref().map(HeaderValue::from_str).transpose() {
                            Ok(cursor) => {
                                let mut response = document_response(document);
                                if let Some(cursor) = cursor {
                                    response.headers_mut().insert("x-next-cursor", cursor);
                                }
                                response
                            }
                            Err(_) => storage_unavailable(),
                        }
                    }
                    Err(_) => storage_unavailable(),
                }
            } else {
                (
                    StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({"detail":errors})),
                )
                    .into_response()
            }
        } else {
            storage_unavailable()
        }
    } else {
        unauthorized()
    };
    private_cache(response, &parts.headers)
}

fn positive(
    input: Option<&str>,
    field: &str,
    max: Option<u16>,
    errors: &mut Vec<Value>,
) -> Option<String> {
    let input = input?;
    let parsed = integer_argument(&Value::String(input.to_owned())).map(|value| value.to_string());
    let Some(parsed) = parsed else {
        errors.push(json!({
            "type":"int_parsing","loc":["query",field],
            "msg":"Input should be a valid integer, unable to parse string as an integer",
            "input":input,
        }));
        return None;
    };
    if parsed == "0" || parsed.starts_with('-') {
        errors.push(json!({
            "type":"greater_than_equal","loc":["query",field],
            "msg":"Input should be greater than or equal to 1","input":input,"ctx":{"ge":1}
        }));
        return None;
    }
    if let Some(max) = max
        && !parsed.parse::<u16>().is_ok_and(|value| value <= max)
    {
        errors.push(json!({
            "type":"less_than_equal","loc":["query",field],
            "msg":format!("Input should be less than or equal to {max}"),"input":input,"ctx":{"le":max}
        }));
        return None;
    }
    Some(parsed)
}
