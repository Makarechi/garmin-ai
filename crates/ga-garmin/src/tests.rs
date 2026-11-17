use std::{
    io,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

use axum::{
    Router,
    extract::{Request, State},
    response::{IntoResponse, Response as TestResponse},
};
use reqwest::{Method, header::LOCATION};
use tokio::{net::TcpListener, task::JoinHandle};

use super::*;

const KEY: &str = "synthetic-personal-key";
const SETTINGS_PATH: &str = "/userprofile-service/userprofile/settings";
const DOWNLOAD_PATH: &str = "/download-service/files/activity/42";
const JSON: &[u8] = br#"{ "unknownField" : [1, "value"] }"#;
const ACTIVITY: &[u8] = b"PK\x03\x04synthetic activity contents";

struct Fixture {
    tokens: AtomicUsize,
    data: AtomicUsize,
    mode: AtomicUsize,
    token: std::sync::Mutex<String>,
}

impl Fixture {
    fn new() -> Self {
        Self {
            tokens: AtomicUsize::new(0),
            data: AtomicUsize::new(0),
            mode: AtomicUsize::new(0),
            token: std::sync::Mutex::new(String::new()),
        }
    }

    fn mode(&self, value: usize) {
        self.mode.store(value, Ordering::Relaxed);
    }

    fn counts(&self) -> (usize, usize) {
        (
            self.tokens.load(Ordering::Relaxed),
            self.data.load(Ordering::Relaxed),
        )
    }
}

struct Server {
    base: Url,
    task: JoinHandle<()>,
}

impl Server {
    async fn start(app: Router) -> Result<Self, Box<dyn Error>> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let base = Url::parse(&format!("http://{}/", listener.local_addr()?))?;
        let task = tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        Ok(Self { base, task })
    }

    fn client(&self) -> Result<PersonalClient, GarminError> {
        PersonalClient::with_base(KEY, self.base.clone(), test_config())
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn test_config() -> TransportConfig {
    TransportConfig {
        pacing: Duration::ZERO,
        connect_timeout: Duration::from_secs(2),
        read_timeout: Duration::from_secs(2),
        total_timeout: Duration::from_secs(3),
    }
}

async fn fixture() -> Result<(Arc<Fixture>, Server), Box<dyn Error>> {
    let state = Arc::new(Fixture::new());
    let app = Router::new().fallback(handle).with_state(state.clone());
    Ok((state, Server::start(app).await?))
}

fn jwt(exp: u64, issue: usize) -> String {
    let claims = URL_SAFE_NO_PAD.encode(format!(r#"{{"exp":{exp},"sub":{issue}}}"#));
    format!("eyJhbGciOiJSUzI1NiJ9.{claims}.synthetic")
}

fn expiry() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        + 900
}

async fn handle(State(state): State<Arc<Fixture>>, request: Request) -> TestResponse {
    let auth = request
        .headers()
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    let path = request.uri().path();
    let mode = state.mode.load(Ordering::Relaxed);
    if path.strip_prefix('/') == Some(TOKEN_PATH) {
        state.tokens.fetch_add(1, Ordering::Relaxed);
        if *request.method() != Method::POST || auth != Some(&format!("ApiKey {KEY}")) {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        if mode == 11 {
            return "invalid synthetic secret token".into_response();
        }
        let token = jwt(expiry(), state.counts().0);
        if let Ok(mut issued) = state.token.lock() {
            *issued = token.clone();
        }
        return token.into_response();
    }
    state.data.fetch_add(1, Ordering::Relaxed);
    if !state
        .token
        .lock()
        .is_ok_and(|issued| auth == Some(&format!("Bearer {issued}")))
    {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    if *request.method() != Method::GET {
        return StatusCode::METHOD_NOT_ALLOWED.into_response();
    }
    match mode {
        1 if state.counts().0 == 1 => StatusCode::UNAUTHORIZED.into_response(),
        2 => StatusCode::UNAUTHORIZED.into_response(),
        3 => StatusCode::FORBIDDEN.into_response(),
        4 => (
            StatusCode::TOO_MANY_REQUESTS,
            [(RETRY_AFTER, "73")],
            "synthetic rate limit",
        )
            .into_response(),
        5 => (StatusCode::BAD_GATEWAY, KEY).into_response(),
        6 => StatusCode::NO_CONTENT.into_response(),
        7 => "[invalid JSON".into_response(),
        8 => StatusCode::PARTIAL_CONTENT.into_response(),
        9 => {
            tokio::time::sleep(Duration::from_secs(1)).await;
            JSON.into_response()
        }
        10 => "null".into_response(),
        12 => b"\"\xff\"".as_slice().into_response(),
        _ if path == SETTINGS_PATH => JSON.into_response(),
        _ if path == DOWNLOAD_PATH => ACTIVITY.into_response(),
        _ => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn settings(client: &PersonalClient) -> Result<Option<Vec<u8>>, GarminError> {
    client.get_json(PersonalRead::ProfileSettings, 1024).await
}

#[tokio::test]
async fn concurrent_reads_share_one_exchange_and_refresh_rejected_cached_token_once()
-> Result<(), Box<dyn Error>> {
    let (state, server) = fixture().await?;
    let client = server.client()?;
    let mut readers = tokio::task::JoinSet::new();
    for _ in 0..12 {
        let clone = client.clone();
        readers.spawn(async move { settings(&clone).await });
    }
    while let Some(result) = readers.join_next().await {
        assert_eq!(result??.as_deref(), Some(JSON));
    }
    assert_eq!(state.counts(), (1, 12));

    state.mode(1);
    assert_eq!(settings(&client).await?.as_deref(), Some(JSON));
    assert_eq!(state.counts(), (2, 14));

    state.mode(2);
    assert_eq!(settings(&client).await, Err(GarminError::Unauthorized));
    assert_eq!(state.counts(), (3, 16));
    assert_eq!(settings(&client).await, Err(GarminError::Unauthorized));
    assert_eq!(state.counts(), (4, 17));
    Ok(())
}

#[tokio::test]
async fn renews_an_expiring_token_and_paces_each_request() -> Result<(), Box<dyn Error>> {
    let (state, server) = fixture().await?;
    let pacing = Duration::from_millis(20);
    let client = PersonalClient::with_base(
        KEY,
        server.base.clone(),
        TransportConfig {
            pacing,
            ..test_config()
        },
    )?;
    let start = Instant::now();
    assert_eq!(settings(&client).await?.as_deref(), Some(JSON));
    assert!(start.elapsed() >= pacing);
    let renewal = {
        let mut session = client.inner.state.lock().await;
        let token = session
            .token
            .as_mut()
            .ok_or(GarminError::InvalidTokenResponse)?;
        token.renew_at = Instant::now();
        session.next_request
    };
    let (first, second) = tokio::join!(settings(&client), settings(&client));
    assert_eq!(first?.as_deref(), Some(JSON));
    assert_eq!(second?.as_deref(), Some(JSON));
    assert!(Instant::now() >= renewal + pacing * 2);
    assert_eq!(state.counts(), (2, 3));
    Ok(())
}

#[tokio::test]
async fn cooldown_and_status_errors_do_not_leak_upstream_body_or_repeat_calls()
-> Result<(), Box<dyn Error>> {
    let (state, server) = fixture().await?;
    let client = server.client()?;

    state.mode(3);
    assert_eq!(settings(&client).await, Err(GarminError::Forbidden));
    state.mode(5);
    let failure = settings(&client).await;
    assert_eq!(failure, Err(GarminError::Upstream(502)));
    assert!(!format!("{failure:?}").contains(KEY));
    assert!(!GarminError::Upstream(502).to_string().contains(KEY));
    state.mode(4);
    assert_eq!(
        settings(&client).await,
        Err(GarminError::RateLimited {
            retry_after: Duration::from_secs(73)
        })
    );
    state.mode(0);
    assert!(matches!(
        settings(&client.clone()).await,
        Err(GarminError::RateLimited { retry_after }) if retry_after > Duration::from_secs(70)
    ));
    assert_eq!(state.counts(), (1, 3));
    assert_eq!(
        parse_retry_after("18446744073709551615"),
        Some(Duration::MAX - Duration::from_nanos(999_999_999))
    );
    assert_eq!(
        parse_retry_after("Sat, 01 Jan 2000 00:00:00 GMT"),
        Some(Duration::ZERO)
    );
    assert_eq!(parse_retry_after("invalid"), None);
    Ok(())
}

#[tokio::test]
async fn original_bytes_and_empty_unsupported_or_invalid_responses_are_distinct()
-> Result<(), Box<dyn Error>> {
    let (state, server) = fixture().await?;
    let client = server.client()?;
    let activity = NonZeroU64::new(42).ok_or_else(|| io::Error::other("Invalid fixture ID"))?;
    let capability = LegacyCapability::TrainingReadiness;
    assert_eq!(
        client
            .get_json(PersonalRead::LegacyOnly(capability), 1024)
            .await,
        Err(GarminError::UnsupportedCapability(capability))
    );
    assert_eq!(state.counts(), (0, 0));
    assert_eq!(settings(&client).await?.as_deref(), Some(JSON));
    assert_eq!(
        client.get_json(PersonalRead::ProfileSettings, 3).await,
        Err(GarminError::ResponseTooLarge)
    );
    let mut destination = Vec::new();
    assert_eq!(
        client
            .download_activity(activity, ACTIVITY.len() as u64, &mut destination)
            .await?,
        ACTIVITY.len() as u64
    );
    assert_eq!(destination, ACTIVITY);
    state.mode(6);
    assert_eq!(settings(&client).await?, None);
    assert_eq!(
        client
            .download_activity(activity, 1024, &mut Vec::new())
            .await,
        Err(GarminError::EmptyDownload)
    );
    state.mode(10);
    assert_eq!(
        settings(&client).await?.as_deref(),
        Some(b"null".as_slice())
    );
    for mode in [7, 12] {
        state.mode(mode);
        assert_eq!(settings(&client).await, Err(GarminError::InvalidJson));
    }
    state.mode(8);
    assert_eq!(
        client
            .download_activity(activity, 1024, &mut Vec::new())
            .await,
        Err(GarminError::InterruptedTransfer)
    );
    Ok(())
}

#[tokio::test]
async fn malformed_or_expired_token_is_rejected_and_read_timeout_is_bounded()
-> Result<(), Box<dyn Error>> {
    let (state, server) = fixture().await?;
    let client = PersonalClient::with_base(
        KEY,
        server.base.clone(),
        TransportConfig {
            read_timeout: Duration::from_millis(150),
            ..test_config()
        },
    )?;
    state.mode(11);
    assert_eq!(
        settings(&client).await,
        Err(GarminError::InvalidTokenResponse)
    );
    assert_eq!(state.counts(), (1, 0));
    assert!(matches!(
        token_from_response(jwt(1, 1).as_bytes()),
        Err(GarminError::InvalidTokenResponse)
    ));
    state.mode(9);
    assert_eq!(settings(&client).await, Err(GarminError::Timeout));
    assert_eq!(state.counts(), (2, 1));
    Ok(())
}

#[tokio::test]
async fn redirect_never_sends_key_to_another_destination() -> Result<(), Box<dyn Error>> {
    let destinations = Arc::new(AtomicUsize::new(0));
    let hits = destinations.clone();
    let target = Server::start(Router::new().fallback(move || {
        let hits = hits.clone();
        async move {
            hits.fetch_add(1, Ordering::Relaxed);
            StatusCode::OK
        }
    }))
    .await?;
    let location = target.base.to_string();
    let origin = Server::start(Router::new().fallback(move || {
        let location = location.clone();
        async move { (StatusCode::TEMPORARY_REDIRECT, [(LOCATION, location)]) }
    }))
    .await?;
    assert_eq!(
        settings(&origin.client()?).await,
        Err(GarminError::Redirect)
    );
    assert_eq!(destinations.load(Ordering::Relaxed), 0);
    Ok(())
}

#[tokio::test]
async fn rejects_chunked_overflow_and_premature_close() -> Result<(), Box<dyn Error>> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let base = Url::parse(&format!("http://{}/", listener.local_addr()?))?;
    let task = tokio::spawn(async move {
        use tokio::io::{AsyncBufReadExt, BufReader};
        let token = jwt(expiry(), 1);
        let exchange = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            token.len(),
            token
        );
        let responses: [&[u8]; 3] = [
            exchange.as_bytes(),
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n4\r\n0123\r\n4\r\n4567\r\n0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\n123",
        ];
        for response in responses {
            let (socket, _) = listener.accept().await?;
            let mut reader = BufReader::new(socket);
            let mut line = String::new();
            loop {
                if reader.read_line(&mut line).await? == 0 || line == "\r\n" {
                    break;
                }
                line.clear();
            }
            let mut socket = reader.into_inner();
            socket.write_all(response).await?;
            socket.shutdown().await?;
        }
        Ok::<_, io::Error>(())
    });
    let server = Server {
        base,
        task: tokio::spawn(async move {
            let _ = task.await;
        }),
    };
    let client = server.client()?;
    let activity = NonZeroU64::new(42).ok_or_else(|| io::Error::other("Invalid fixture ID"))?;
    let mut output = Vec::new();
    assert_eq!(
        client.download_activity(activity, 6, &mut output).await,
        Err(GarminError::ResponseTooLarge)
    );
    assert_eq!(output, b"0123");
    output.clear();
    assert_eq!(
        client.download_activity(activity, 15, &mut output).await,
        Err(GarminError::InterruptedTransfer)
    );
    Ok(())
}

#[tokio::test]
#[ignore = "start the local Garmin Personal API Postman simulation"]
async fn local_postman_simulation() -> Result<(), Box<dyn Error>> {
    let base = Url::parse(&std::env::var("GA_TEST_GARMIN_SIM_URL")?)?;
    assert_eq!(base.scheme(), "http");
    assert_eq!(base.host_str(), Some("127.0.0.1"));
    assert_eq!(base.path(), "/garmin-personal/");
    let client = PersonalClient::with_base(KEY, base, test_config())?;
    let mut data = Vec::new();
    assert_eq!(settings(&client).await?.as_deref(), Some(JSON));
    client
        .download_activity(NonZeroU64::try_from(42)?, 1024, &mut data)
        .await?;
    assert_eq!(data, ACTIVITY);
    assert_eq!(
        client
            .get_json(PersonalRead::Activity(NonZeroU64::try_from(403)?), 1024)
            .await,
        Err(GarminError::Forbidden)
    );
    assert_eq!(
        client
            .get_json(PersonalRead::Activity(NonZeroU64::try_from(429)?), 1024)
            .await,
        Err(GarminError::RateLimited {
            retry_after: Duration::from_secs(73)
        })
    );
    assert!(matches!(
        settings(&client).await,
        Err(GarminError::RateLimited { .. })
    ));
    Ok(())
}
