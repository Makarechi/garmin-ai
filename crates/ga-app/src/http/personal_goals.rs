use super::{
    AppState, method_not_allowed, private_cache,
    tools::{
        MAX_REQUEST_BYTES, detail, forbidden, invalid_arguments, storage_unavailable, unauthorized,
    },
};
use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::{
    Database,
    personal_goals::{GoalSelection, PersonalGoal, PersonalGoalsError},
};
use ga_domain::{access::Scope, timestamp::StoredInstant};
use serde_json::{Value, json};
use time::OffsetDateTime;
use uuid::Uuid;

const EXPECTED_GOAL: &str = "'wellbeing', 'sleep', 'running' or 'migraine'";

pub(super) async fn preferences(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = if matches!(parts.method, Method::GET | Method::PUT) {
        let required: &[_] = if parts.method == Method::GET {
            &[Scope::ReadDiary]
        } else {
            &[Scope::ReadDiary, Scope::WriteDiary]
        };
        if let Some(scopes) = state
            .authorization
            .authenticate(parts.headers.get(header::AUTHORIZATION))
        {
            if !scopes.permits(required) {
                forbidden()
            } else if let Some(database) = state.database.as_ref() {
                match database.enrolled_owner_id().await {
                    Ok(owner) if parts.method == Method::PUT => store(database, owner, body).await,
                    Ok(owner) => match database.bound_personal_goals(owner).await {
                        Ok(value) => Json(json!(value)).into_response(),
                        Err(error) => failure(error),
                    },
                    Err(error) => failure(error),
                }
            } else {
                storage_unavailable()
            }
        } else {
            unauthorized()
        }
    } else {
        method_not_allowed("GET")
    };
    private_cache(response, &parts.headers)
}

async fn store(database: &Database, owner: Uuid, body: Body) -> Response {
    let data = match to_bytes(body, MAX_REQUEST_BYTES).await {
        Ok(data) => data,
        Err(_) => {
            return detail(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Goal preferences request exceeds the size limit",
            );
        }
    };
    let value = if data.is_empty() {
        Value::Null
    } else {
        let input = data.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&data);
        match serde_json::from_slice(input) {
            Ok(value) => value,
            Err(_) => return invalid_arguments(),
        }
    };
    let (revision, goals) = match validate(value) {
        Ok(value) => value,
        Err(errors) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail":errors})),
            )
                .into_response();
        }
    };
    let micros = OffsetDateTime::now_utc()
        .unix_timestamp_nanos()
        .div_euclid(1000);
    let Some(at) = i64::try_from(micros)
        .ok()
        .and_then(|value| StoredInstant::from_unix_microseconds(value).ok())
    else {
        return storage_unavailable();
    };
    match database
        .select_personal_goals(
            owner,
            GoalSelection {
                expected_revision: revision,
                goals: &goals,
                now: at,
                command: None,
            },
        )
        .await
    {
        Ok(value) => Json(json!(value)).into_response(),
        Err(error) => failure(error),
    }
}

fn validate(value: Value) -> Result<(u64, Vec<PersonalGoal>), Vec<Value>> {
    let Some(fields) = value.as_object() else {
        return Err(vec![if value.is_null() {
            json!({"type":"missing","loc":["body"],"msg":"Field required","input":null})
        } else {
            json!({"type":"model_attributes_type","loc":["body"],"msg":"Input should be a valid dictionary or object to extract fields from","input":value})
        }]);
    };
    let mut errors = Vec::new();
    let revision = match fields.get("revision") {
        Some(value) if value.as_u64().is_some() => value.as_u64(),
        Some(Value::Number(number)) if number.as_str() == "-0" => Some(0),
        Some(Value::Number(number))
            if number
                .to_string()
                .strip_prefix('-')
                .is_some_and(|digits| digits.bytes().all(|byte| byte.is_ascii_digit())) =>
        {
            errors.push(
                json!({"type":"greater_than_equal","loc":["body","revision"],
                "msg":"Input should be greater than or equal to 0","input":number,"ctx":{"ge":0}}),
            );
            None
        }
        Some(Value::Number(number))
            if number.to_string().bytes().all(|byte| byte.is_ascii_digit()) =>
        {
            // Positive values beyond the stored revision range cannot match it.
            Some(u64::MAX)
        }
        Some(input) => {
            errors.push(json!({"type":"int_type","loc":["body","revision"],"msg":"Input should be a valid integer","input":input}));
            None
        }
        None => {
            errors.push(json!({"type":"missing","loc":["body","revision"],"msg":"Field required","input":value}));
            None
        }
    };
    let mut goals = Vec::new();
    match fields.get("goals"){
        Some(Value::Array(values)) if values.len()>4=>{
            errors.push(json!({"type":"too_long","loc":["body","goals"],"input":values,
                "msg":format!("List should have at most 4 items after validation, not {}",values.len()),
                "ctx":{"field_type":"List","max_length":4,"actual_length":values.len()}}));
        }
        Some(Value::Array(values))=>{
            for (index,input) in values.iter().enumerate(){
                let goal=match input.as_str(){
                    Some("wellbeing")=>PersonalGoal::Wellbeing,
                    Some("sleep")=>PersonalGoal::Sleep,
                    Some("running")=>PersonalGoal::Running,
                    Some("migraine")=>PersonalGoal::Migraine,
                    _=>{
                        errors.push(json!({"type":"literal_error","loc":["body","goals",index],
                            "msg":format!("Input should be {EXPECTED_GOAL}"),"input":input,
                            "ctx":{"expected":EXPECTED_GOAL}}));
                        continue
                    }
                };
                goals.push(goal);
            }
        }
        Some(input)=>errors.push(json!({"type":"list_type","loc":["body","goals"],"msg":"Input should be a valid list","input":input})),
        None=>errors.push(json!({"type":"missing","loc":["body","goals"],"msg":"Field required","input":value})),
    }
    for (key, input) in fields
        .iter()
        .filter(|(key, _)| !matches!(key.as_str(), "revision" | "goals"))
    {
        errors.push(json!({"type":"extra_forbidden","loc":["body",key],"msg":"Extra inputs are not permitted","input":input}));
    }
    if errors.is_empty() && (0..goals.len()).any(|index| goals[..index].contains(&goals[index])) {
        errors.push(
            json!({"type":"value_error","loc":["body"],"msg":"Value error, Goals must be distinct",
            "input":value,"ctx":{"error":{}}}),
        );
    }
    if let Some(revision) = revision.filter(|_| errors.is_empty()) {
        Ok((revision, goals))
    } else {
        Err(errors)
    }
}

fn failure(error: PersonalGoalsError) -> Response {
    match error {
        PersonalGoalsError::Conflict => detail(
            StatusCode::CONFLICT,
            "Goal preferences changed; reload before updating",
        ),
        PersonalGoalsError::InvalidSelection => invalid_arguments(),
        _ => storage_unavailable(),
    }
}
