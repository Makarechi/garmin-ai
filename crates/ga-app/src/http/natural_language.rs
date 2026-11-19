use axum::{
    Json,
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::diary::actions::{TrackerTextContext, TrackerTextError};
use ga_domain::{timestamp::StoredInstant, tracker_nl::TrackerTextRequest};

use super::{
    AppState,
    diary::{access_failure, authorized},
    method_not_allowed, private_cache,
    tools::{MAX_REQUEST_BYTES, invalid_arguments, unauthorized},
};

pub(super) async fn tracker(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = if parts.method != Method::POST {
        method_not_allowed("POST")
    } else if let Some(scopes) = state
        .authorization
        .authenticate(parts.headers.get(header::AUTHORIZATION))
    {
        match authorized(&state, &parts.headers, &[]).await {
            Ok((database, owner)) => {
                let result = async {
                    let bytes = to_bytes(body, MAX_REQUEST_BYTES)
                        .await
                        .map_err(|_| TrackerTextError::Invalid)?;
                    let bytes = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
                    let raw =
                        serde_json::from_slice(bytes).map_err(|_| TrackerTextError::Invalid)?;
                    let request = TrackerTextRequest::from_value(&raw)
                        .map_err(|_| TrackerTextError::Invalid)?;
                    let micros = time::OffsetDateTime::now_utc()
                        .unix_timestamp_nanos()
                        .div_euclid(1000);
                    let message_at = StoredInstant::from_unix_microseconds(
                        i64::try_from(micros).map_err(|_| TrackerTextError::Database)?,
                    )
                    .map_err(|_| TrackerTextError::Database)?;
                    run_tracker(
                        database,
                        &request,
                        TrackerTextContext {
                            owner,
                            scopes,
                            actor: "api",
                            message_at,
                            configured_timezone: &state.configured_timezone,
                            configured_locale: &state.configured_locale,
                        },
                    )
                    .await
                }
                .await;
                match result {
                    Ok(result) => Json(result).into_response(),
                    Err(TrackerTextError::Invalid) => invalid_arguments(),
                    Err(TrackerTextError::Form(error)) => {
                        super::tracker_actions::submit_failure(error)
                    }
                    Err(error) => (
                        match error {
                            TrackerTextError::Forbidden => StatusCode::FORBIDDEN,
                            TrackerTextError::Missing => StatusCode::NOT_FOUND,
                            _ => StatusCode::SERVICE_UNAVAILABLE,
                        },
                        Json(serde_json::json!({"detail":error.to_string()})),
                    )
                        .into_response(),
                }
            }
            Err(status) => access_failure(status),
        }
    } else {
        unauthorized()
    };
    private_cache(response, &parts.headers)
}

async fn run_tracker(
    database: &ga_db::Database,
    request: &TrackerTextRequest,
    context: TrackerTextContext<'_>,
) -> Result<serde_json::Value, TrackerTextError> {
    let settings = crate::replay_worker::model_settings();
    let configured = settings
        .as_ref()
        .map(ga_app::model_gateway::RuntimeModelSettings::configured);
    if let Some(configured) = configured
        && database
            .model_onboarding_allows(ga_domain::model_consent::ModelCategories::DIARY)
            .await
            .unwrap_or(false)
        && let Ok(gateway) = configured.gateway(database.clone(), false, context.message_at)
    {
        return gateway
            .process_tracker_text(request, context, || configured.current())
            .await;
    }
    database.tracker_text_fallback(request, context).await
}
