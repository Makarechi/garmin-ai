use std::io;

use axum::{
    Router,
    body::Bytes,
    extract::State,
    http::{Method, StatusCode, Uri, header},
    response::{IntoResponse, Response},
};
use tokio::net::TcpListener;

const PAGE: &str = include_str!("../../../../src/garmin_ai/static/dashboard/index.html");
const SCRIPT: &[u8] = include_bytes!("../../../../src/garmin_ai/static/dashboard/app.js");
const STYLES: &[u8] = include_bytes!("../../../../src/garmin_ai/static/dashboard/styles.css");
const CONTENT_POLICY: &str = concat!(
    "default-src 'none'; script-src 'self'; style-src 'self'; ",
    "connect-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
);

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn serve(host: &str, port: u16) -> io::Result<()> {
    let listener = TcpListener::bind((host, port)).await?;
    let doc = PAGE.replace(
        r#"<button id="connect""#,
        r#"<button id="connect" hidden disabled"#,
    );
    let app = Router::new().fallback(asset).with_state(Bytes::from(doc));
    println!("Open http://{}/dashboard", listener.local_addr()?);
    super::accept(listener, app).await
}

async fn asset(State(page): State<Bytes>, method: Method, uri: Uri) -> Response {
    if method != Method::GET {
        return StatusCode::NOT_IMPLEMENTED.into_response();
    }
    let (content_type, bytes) = match uri.path_and_query().map(|target| target.as_str()) {
        Some("/dashboard") => ("text/html; charset=utf-8", page),
        Some("/dashboard-assets/app.js") => {
            ("text/javascript; charset=utf-8", Bytes::from_static(SCRIPT))
        }
        Some("/dashboard-assets/styles.css") => {
            ("text/css; charset=utf-8", Bytes::from_static(STYLES))
        }
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    (
        [
            (header::CONTENT_TYPE, content_type),
            (header::CACHE_CONTROL, "no-store"),
            (header::REFERRER_POLICY, "no-referrer"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
            (header::CONTENT_SECURITY_POLICY, CONTENT_POLICY),
        ],
        bytes,
    )
        .into_response()
}
