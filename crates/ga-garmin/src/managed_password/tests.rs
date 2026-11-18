use std::error::Error;

use axum::{
    Router,
    http::{HeaderMap, StatusCode, header},
    response::IntoResponse,
    routing::get,
};
use tokio::net::TcpListener;

use super::*;

#[tokio::test]
async fn only_reads_the_pinned_version_using_a_bounded_authenticated_response()
-> Result<(), Box<dyn Error>> {
    let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
    let origin = format!("http://{}", listener.local_addr()?);
    let router = Router::new()
        .route(
            "/identity",
            get(|headers: HeaderMap| async move {
                assert_eq!(headers["metadata-flavor"], "Google");
                r#"{"access_token":"synthetic-metadata-identity"}"#
            }),
        )
        .route(
            "/secrets/projects/synthetic/secrets/password/versions/1:access",
            get(|headers: HeaderMap| async move {
                assert_eq!(
                    headers[header::AUTHORIZATION],
                    "Bearer synthetic-metadata-identity"
                );
                r#"{"payload":{"data":"ZmljdGlvbmFsLXBhc3M="}}"#
            }),
        )
        .route(
            "/secrets/projects/synthetic/secrets/password/versions/2:access",
            get(|| async {
                (StatusCode::FOUND, [(header::LOCATION, "/unexpected")]).into_response()
            }),
        )
        .route(
            "/secrets/projects/synthetic/secrets/password/versions/3:access",
            get(|| async { "x".repeat(RESPONSE_LIMIT as usize + 1) }),
        )
        .route(
            "/unexpected",
            get(|| async { r#"{"payload":{"data":"ZmljdGlvbmFsLXBhc3M="}}"# }),
        );
    let server = tokio::spawn(async move { axum::serve(listener, router).await });
    let client = build_http(TransportConfig::default(), false)?.build()?;
    let metadata = format!("{origin}/identity");
    let secrets = format!("{origin}/secrets");
    let version = |number| format!("projects/synthetic/secrets/password/versions/{number}");
    let first = GarminPasswordSecret::pinned(version("1"))?;
    assert_eq!(
        first
            .read_from(&client, &metadata, &secrets)
            .await?
            .as_str(),
        "fictional-pass"
    );
    for rejected in ["2", "3"] {
        let secret = GarminPasswordSecret::pinned(version(rejected))?;
        assert!(
            secret
                .read_from(&client, &metadata, &secrets)
                .await
                .is_err()
        );
    }
    for unpinned in ["latest", "01", "1:access", "1?alt=json", "1/../2"] {
        assert!(GarminPasswordSecret::pinned(version(unpinned)).is_err());
    }
    server.abort();
    Ok(())
}
