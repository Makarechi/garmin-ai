use std::{env, io};

use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::any,
};
use ga_db::{Database, ReadinessError};
use serde_json::json;
use tokio::{net::TcpListener, signal};

fn router(database: Option<Database>) -> Router {
    Router::new()
        .route("/health/live", any(liveness))
        .route("/health/ready", any(readiness))
        .fallback(missing)
        .with_state(database)
}

async fn liveness(method: Method, headers: HeaderMap) -> Response {
    let response = if method == Method::GET {
        Json(json!({"status": "alive"})).into_response()
    } else {
        method_not_allowed()
    };
    private_cache(response, &headers)
}

async fn readiness(
    method: Method,
    headers: HeaderMap,
    State(database): State<Option<Database>>,
) -> Response {
    let response = if method == Method::GET {
        let status = match database {
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
        method_not_allowed()
    };
    private_cache(response, &headers)
}

fn method_not_allowed() -> Response {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        [(header::ALLOW, "GET")],
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response()
}

async fn missing(headers: HeaderMap) -> Response {
    let response = (StatusCode::NOT_FOUND, Json(json!({"detail": "Not Found"}))).into_response();
    private_cache(response, &headers)
}

fn private_cache(mut response: Response, request: &HeaderMap) -> Response {
    if request.contains_key(header::AUTHORIZATION) {
        response
            .headers_mut()
            .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    response
}

#[tokio::main(flavor = "current_thread")]
pub async fn serve(host: &str, port: u16) -> io::Result<()> {
    let listener = TcpListener::bind((host, port)).await?;

    #[cfg(unix)]
    let mut terminate = signal::unix::signal(signal::unix::SignalKind::terminate())?;

    let shutdown = async move {
        #[cfg(unix)]
        {
            tokio::select! {
                _ = signal::ctrl_c() => {},
                _ = terminate.recv() => {},
            }
        }
        #[cfg(not(unix))]
        {
            let _ = signal::ctrl_c().await;
        }
    };

    let database = env::var("GA_DATABASE_URL")
        .ok()
        .and_then(|url| Database::connect_lazy(&url).ok());
    println!("Listening on http://{}", listener.local_addr()?);
    axum::serve(listener, router(database))
        .with_graceful_shutdown(shutdown)
        .await
}
