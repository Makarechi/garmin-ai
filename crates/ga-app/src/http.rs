use std::io;

use axum::{
    Json, Router,
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::any,
};
use serde_json::json;
use tokio::{net::TcpListener, signal};

fn router() -> Router {
    Router::new()
        .route("/health/live", any(liveness))
        .fallback(missing)
}

async fn liveness(method: Method, headers: HeaderMap) -> Response {
    let response = if method == Method::GET {
        Json(json!({"status": "alive"})).into_response()
    } else {
        (
            StatusCode::METHOD_NOT_ALLOWED,
            [(header::ALLOW, "GET")],
            Json(json!({"detail": "Method Not Allowed"})),
        )
            .into_response()
    };
    private_cache(response, &headers)
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

    println!("Listening on http://{}", listener.local_addr()?);
    axum::serve(listener, router())
        .with_graceful_shutdown(shutdown)
        .await
}
