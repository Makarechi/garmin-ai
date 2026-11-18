use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use ga_db::diary::{DiaryWriteError, WearableError};
use ga_domain::wearable::{MAX_BATCH_BYTES, parse_batch};

use super::{
    AppState, method_not_allowed, private_cache,
    tools::{detail, invalid_arguments},
};

const UNAVAILABLE: &str = "Database unavailable or identity is not ready";

pub(super) async fn marks(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let response = if parts.method == Method::POST {
        accept(&state, &parts.headers, body).await
    } else {
        method_not_allowed("POST")
    };
    private_cache(response, &parts.headers)
}

async fn accept(state: &AppState, headers: &HeaderMap, body: Body) -> Response {
    let Some(device) = state
        .authorization
        .wearable_device(headers.get(header::AUTHORIZATION))
    else {
        return detail(StatusCode::UNAUTHORIZED, "Wearable authentication required");
    };
    let data = match to_bytes(body, MAX_BATCH_BYTES).await {
        Ok(data) => data,
        Err(_) => {
            return detail(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Wearable upload exceeds the size limit",
            );
        }
    };
    let data = data.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&data);
    let Ok(marks) = parse_batch(data) else {
        return invalid_arguments();
    };
    let Some(database) = &state.database else {
        return detail(StatusCode::SERVICE_UNAVAILABLE, UNAVAILABLE);
    };
    let Ok(owner) = database.enrolled_owner_id().await else {
        return detail(StatusCode::SERVICE_UNAVAILABLE, UNAVAILABLE);
    };
    match database.accept_wearable_marks(owner, device, &marks).await {
        Ok(receipt) => Json(receipt).into_response(),
        Err(WearableError::ContentChanged) => detail(
            StatusCode::CONFLICT,
            "Wearable mark ID was already used with different content",
        ),
        Err(WearableError::Diary(DiaryWriteError::TrackingDisabled)) => {
            detail(StatusCode::FORBIDDEN, "Operation not allowed")
        }
        Err(WearableError::Diary(DiaryWriteError::Replay(_))) => detail(
            StatusCode::CONFLICT,
            "Idempotency key already used for different data",
        ),
        Err(WearableError::InvalidBatch | WearableError::Diary(DiaryWriteError::Input(_))) => {
            invalid_arguments()
        }
        Err(WearableError::Diary(_)) => detail(StatusCode::SERVICE_UNAVAILABLE, UNAVAILABLE),
    }
}
