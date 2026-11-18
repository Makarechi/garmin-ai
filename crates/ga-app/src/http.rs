pub(crate) mod demo;
mod diary;
mod exports;
mod personal_goals;
pub(crate) mod tools;
mod tracker_sharing;
mod wearable;

use std::{env, io, sync::Arc};

use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::any,
};
use ga_db::{Database, ReadinessError};
use ga_domain::access::Scope;
use serde_json::json;
use tokio::{net::TcpListener, signal};

use ga_app::telegram_webhook::TelegramWebhook;

use crate::authorization::Authorization;

#[derive(Clone)]
struct AppState {
    database: Option<Database>,
    authorization: Authorization,
    configured_timezone: Arc<str>,
}

fn router(state: AppState) -> Router {
    Router::new()
        .route("/health/live", any(liveness))
        .route("/health/ready", any(readiness))
        .route("/capabilities", any(capabilities))
        .route("/events", any(diary::create))
        .route("/events/undo", any(diary::undo))
        .route("/events/{event_id}", any(diary::entry))
        .route("/exports/diary", any(exports::diary))
        .route("/entries", any(diary::create_custom))
        .route("/entries/{event_id}", any(diary::edit_custom))
        .route("/tools", any(tools::list))
        .route("/tools/{name}", any(tools::execute))
        .route("/preferences/goals", any(personal_goals::preferences))
        .route("/wearable/marks", any(wearable::marks))
        .route(
            "/tracker-sharing-consents",
            any(tracker_sharing::collection),
        )
        .route("/tracker-sharing/consents", any(tracker_sharing::create))
        .route(
            "/tracker-sharing-consents/{definition}/{kind}/{instance}",
            any(tracker_sharing::revoke),
        )
        .fallback(missing)
        .with_state(state)
}

async fn liveness(method: Method, headers: HeaderMap) -> Response {
    let response = if method == Method::GET {
        Json(json!({"status": "alive"})).into_response()
    } else {
        method_not_allowed("GET")
    };
    private_cache(response, &headers)
}

async fn readiness(method: Method, headers: HeaderMap, State(state): State<AppState>) -> Response {
    let response = if method == Method::GET {
        let status = match state.database {
            Some(database) => database.readiness().await,
            None => Err(ReadinessError::Unavailable),
        };
        match status {
            Ok(()) => Json(json!({"status": "ready"})).into_response(),
            Err(error) => {
                let detail = match error {
                    ReadinessError::Unavailable => "Database unavailable or not migrated",
                    ReadinessError::MigrationRequired => "Database migration required",
                    ReadinessError::StorageDisabled => "Storage disabled after erasure",
                };
                (
                    StatusCode::SERVICE_UNAVAILABLE,
                    Json(json!({"detail": detail})),
                )
                    .into_response()
            }
        }
    } else {
        method_not_allowed("GET")
    };
    private_cache(response, &headers)
}

async fn capabilities(
    method: Method,
    headers: HeaderMap,
    State(state): State<AppState>,
) -> Response {
    let response = if method != Method::GET {
        method_not_allowed("GET")
    } else if let Some(scopes) = state
        .authorization
        .authenticate(headers.get(header::AUTHORIZATION))
    {
        Json(json!({
            "read_diary": scopes.permits(&[Scope::ReadDiary]),
            "write_diary": scopes.permits(&[Scope::ReadDiary, Scope::WriteDiary]),
            "manage_definitions": scopes.permits(&[Scope::ManageDefinitions]),
        }))
        .into_response()
    } else {
        (
            StatusCode::UNAUTHORIZED,
            Json(json!({"detail":"Authentication required"})),
        )
            .into_response()
    };
    private_cache(response, &headers)
}

fn method_not_allowed(allowed: &'static str) -> Response {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        [(header::ALLOW, allowed)],
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response()
}

async fn missing(headers: HeaderMap) -> Response {
    let response = (StatusCode::NOT_FOUND, Json(json!({"detail": "Not Found"}))).into_response();
    private_cache(response, &headers)
}

fn private_cache(mut response: Response, request: &HeaderMap) -> Response {
    if request
        .get(header::AUTHORIZATION)
        .is_some_and(|value| !value.is_empty())
    {
        response
            .headers_mut()
            .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    response
}

#[tokio::main(flavor = "current_thread")]
pub async fn serve(host: &str, port: u16) -> io::Result<()> {
    let authorization = Authorization::configured()?;
    let listener = TcpListener::bind((host, port)).await?;

    let database = env::var("GA_DATABASE_URL")
        .ok()
        .and_then(|url| Database::connect_lazy(&url).ok());
    let configured_timezone = env::var("GA_TIMEZONE")
        .ok()
        .or_else(|| {
            dotenvy::from_path_iter(".env")
                .ok()?
                .filter_map(Result::ok)
                .filter(|(name, _)| name.eq_ignore_ascii_case("GA_TIMEZONE"))
                .map(|(_, value)| value)
                .last()
        })
        .unwrap_or_else(|| "Europe/Bratislava".into());
    let webhook = TelegramWebhook::configured(database.clone()).router();
    println!("Listening on http://{}", listener.local_addr()?);
    accept(
        listener,
        router(AppState {
            database,
            authorization,
            configured_timezone: configured_timezone.into(),
        })
        .merge(webhook),
    )
    .await
}

async fn accept(listener: TcpListener, app: Router) -> io::Result<()> {
    #[cfg(unix)]
    let mut terminate = signal::unix::signal(signal::unix::SignalKind::terminate())?;

    let shutdown = async move {
        #[cfg(unix)]
        tokio::select! {
            _ = signal::ctrl_c() => {},
            _ = terminate.recv() => {},
        }
        #[cfg(not(unix))]
        let _ = signal::ctrl_c().await;
    };
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown)
        .await
}
