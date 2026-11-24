use std::io;

use axum::{
    Router,
    body::Bytes,
    extract::State,
    http::{Method, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use tokio::net::TcpListener;

use super::browser::{DASHBOARD_HTML, DASHBOARD_SCRIPT, DASHBOARD_STYLES, asset_response};
const CONTENT_POLICY: &str = concat!(
    "default-src 'none'; script-src 'self'; style-src 'self'; ",
    "connect-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
);

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn serve(host: &str, port: u16) -> io::Result<()> {
    let listener = TcpListener::bind((host, port)).await?;
    let doc = DASHBOARD_HTML.replace(
        r#"<button id="connect""#,
        r#"<button id="connect" hidden disabled"#,
    );
    let app = Router::new().fallback(asset).with_state(Bytes::from(doc));
    println!("Open http://{}/dashboard", listener.local_addr()?);
    super::accept(listener, app, || {}).await
}

async fn asset(State(page): State<Bytes>, method: Method, uri: Uri) -> Response {
    if method != Method::GET {
        return StatusCode::NOT_IMPLEMENTED.into_response();
    }
    let (content_type, bytes) = match uri
        .path_and_query()
        .map(axum::http::uri::PathAndQuery::as_str)
    {
        Some("/dashboard") => ("text/html; charset=utf-8", page),
        Some("/dashboard-assets/app.js") => (
            "text/javascript; charset=utf-8",
            Bytes::from_static(DASHBOARD_SCRIPT),
        ),
        Some("/dashboard-assets/styles.css") => (
            "text/css; charset=utf-8",
            Bytes::from_static(DASHBOARD_STYLES),
        ),
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    asset_response(content_type, bytes, CONTENT_POLICY)
}
