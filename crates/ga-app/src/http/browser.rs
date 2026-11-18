use axum::{
    Json, Router,
    body::Bytes,
    extract::Path,
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{IntoResponse, Response},
    routing::any,
};
use serde_json::json;

use super::{AppState, method_not_allowed, private_cache};

pub(super) const DASHBOARD_HTML: &str =
    include_str!("../../../../src/garmin_ai/static/dashboard/index.html");
pub(super) const DASHBOARD_SCRIPT: &[u8] =
    include_bytes!("../../../../src/garmin_ai/static/dashboard/app.js");
pub(super) const DASHBOARD_STYLES: &[u8] =
    include_bytes!("../../../../src/garmin_ai/static/dashboard/styles.css");
const CHAT_HTML: &[u8] = include_bytes!("../../../../src/garmin_ai/static/web_chat/index.html");
const CHAT_SCRIPT: &[u8] = include_bytes!("../../../../src/garmin_ai/static/web_chat/app.js");
const CHAT_STYLES: &[u8] = include_bytes!("../../../../src/garmin_ai/static/web_chat/styles.css");
const LIVE_POLICY: &str = concat!(
    "default-src 'none'; script-src 'self'; style-src 'self'; ",
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
);

pub(super) fn routes() -> Router<AppState> {
    Router::new()
        .route("/dashboard", any(dashboard))
        .route("/dashboard-assets/{asset}", any(dashboard_asset))
        .route("/chat", any(chat))
        .route("/chat-assets/{asset}", any(chat_asset))
        .route("/dashboard/", any(redirect))
        .route("/dashboard-assets/{asset}/", any(redirect))
        .route("/chat/", any(redirect))
        .route("/chat-assets/{asset}/", any(redirect))
}

async fn dashboard(method: Method, headers: HeaderMap) -> Response {
    live_asset(
        method,
        &headers,
        Some(("text/html; charset=utf-8", DASHBOARD_HTML.as_bytes())),
    )
}

async fn chat(method: Method, headers: HeaderMap) -> Response {
    live_asset(
        method,
        &headers,
        Some(("text/html; charset=utf-8", CHAT_HTML)),
    )
}

async fn dashboard_asset(Path(path): Path<String>, method: Method, headers: HeaderMap) -> Response {
    live_asset(
        method,
        &headers,
        asset(&path, DASHBOARD_SCRIPT, DASHBOARD_STYLES),
    )
}

async fn chat_asset(Path(path): Path<String>, method: Method, headers: HeaderMap) -> Response {
    live_asset(method, &headers, asset(&path, CHAT_SCRIPT, CHAT_STYLES))
}

fn asset(
    route: &str,
    script: &'static [u8],
    styles: &'static [u8],
) -> Option<(&'static str, &'static [u8])> {
    match route {
        "app.js" => Some(("text/javascript; charset=utf-8", script)),
        "styles.css" => Some(("text/css; charset=utf-8", styles)),
        _ => None,
    }
}

fn live_asset(
    method: Method,
    headers: &HeaderMap,
    asset: Option<(&'static str, &'static [u8])>,
) -> Response {
    let response = if method != Method::GET {
        method_not_allowed("GET")
    } else if let Some((mime, bytes)) = asset {
        asset_response(mime, Bytes::from_static(bytes), LIVE_POLICY)
    } else {
        (StatusCode::NOT_FOUND, Json(json!({"detail":"Not Found"}))).into_response()
    };
    private_cache(response, headers)
}

async fn redirect(uri: Uri, headers: HeaderMap) -> Response {
    let path = uri.path().trim_end_matches('/');
    let location = uri
        .query()
        .map_or_else(|| path.to_owned(), |query| format!("{path}?{query}"));
    let response = (
        StatusCode::TEMPORARY_REDIRECT,
        [(header::LOCATION, location)],
    )
        .into_response();
    private_cache(response, &headers)
}

pub(super) fn asset_response(mime: &'static str, bytes: Bytes, policy: &'static str) -> Response {
    (
        [
            (header::CONTENT_TYPE, mime),
            (header::CACHE_CONTROL, "no-store"),
            (header::REFERRER_POLICY, "no-referrer"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
            (header::CONTENT_SECURITY_POLICY, policy),
        ],
        bytes,
    )
        .into_response()
}
