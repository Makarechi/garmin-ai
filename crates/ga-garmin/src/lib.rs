pub mod legacy;
pub mod managed_password;
mod query;

#[cfg(test)]
mod tests;

use std::{
    error::Error,
    fmt,
    num::NonZeroU64,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use reqwest::{
    Client, ClientBuilder, RequestBuilder, Response, StatusCode, Url,
    header::{ACCEPT, AUTHORIZATION, HeaderValue, RETRY_AFTER},
};
use serde::Deserialize;
use tokio::{
    io::{AsyncWrite, AsyncWriteExt},
    sync::Mutex,
    time::{Instant, sleep_until},
};

pub use query::{DateRange, LegacyCapability, PersonalRead};

const GARMIN_ORIGIN: &str = "https://personal.connectapi.garmin.com/";
const TOKEN_PATH: &str = "personalapitoken-service/personal-api/personal-api-key/token";
const MAX_TOKEN_BYTES: u64 = 64 * 1024;

#[derive(Clone, Copy, Debug)]
pub struct TransportConfig {
    pub pacing: Duration,
    pub connect_timeout: Duration,
    pub read_timeout: Duration,
    pub total_timeout: Duration,
}

impl Default for TransportConfig {
    fn default() -> Self {
        Self {
            pacing: Duration::from_secs(2),
            connect_timeout: Duration::from_secs(10),
            read_timeout: Duration::from_secs(15),
            total_timeout: Duration::from_secs(90),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GarminError {
    InvalidConfiguration,
    InvalidRequest,
    UnsupportedCapability(LegacyCapability),
    InvalidTokenResponse,
    AuthenticationRequired,
    InvalidMfaCode,
    MfaExpired,
    ProfileUnavailable,
    OwnerMismatch,
    TokenStorage,
    Unauthorized,
    Forbidden,
    RateLimited { retry_after: Duration },
    Redirect,
    Upstream(u16),
    UnexpectedStatus(u16),
    Timeout,
    Network,
    InterruptedTransfer,
    ResponseTooLarge,
    EmptyDownload,
    InvalidJson,
    DestinationWrite,
}

impl fmt::Display for GarminError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidConfiguration => "Garmin client configuration is invalid",
            Self::InvalidRequest => "Garmin request parameters are invalid",
            Self::UnsupportedCapability(_) => "Personal API does not support this capability",
            Self::InvalidTokenResponse => "Garmin token response is invalid",
            Self::AuthenticationRequired => "Garmin authentication is required",
            Self::InvalidMfaCode => "Garmin verification code was rejected",
            Self::MfaExpired => "Garmin verification session expired",
            Self::ProfileUnavailable => "Authenticated stable Garmin profile is unavailable",
            Self::OwnerMismatch => "Garmin profile differs from the expected owner",
            Self::TokenStorage => "Garmin token storage is unavailable",
            Self::Unauthorized => "Garmin rejected authorization",
            Self::Forbidden => "Garmin refused this resource",
            Self::RateLimited { .. } => "Garmin is rate limiting requests",
            Self::Redirect => "Garmin returned an unexpected redirect",
            Self::Upstream(_) => "Garmin returned a server error",
            Self::UnexpectedStatus(_) => "Garmin returned an unexpected status",
            Self::Timeout => "Garmin request timed out",
            Self::Network => "Garmin connection failed",
            Self::InterruptedTransfer => "Garmin response body ended unexpectedly",
            Self::ResponseTooLarge => "Garmin response exceeds the byte limit",
            Self::EmptyDownload => "Garmin activity download is empty",
            Self::InvalidJson => "Garmin response is not valid JSON",
            Self::DestinationWrite => "Could not write the Garmin download",
        })
    }
}

impl Error for GarminError {}

#[derive(Clone)]
pub struct PersonalClient {
    inner: Arc<Inner>,
}

struct Inner {
    http: Client,
    base: Url,
    api_key: HeaderValue,
    pacing: Duration,
    state: Mutex<Session>,
}

struct Session {
    token: Option<Token>,
    schedule: Schedule,
}

struct Schedule {
    next_request: Instant,
    throttle: Option<(Instant, Duration)>,
}

struct Token {
    authorization: HeaderValue,
    renew_at: Instant,
}

#[derive(Deserialize)]
struct Claims {
    exp: u64,
}

impl PersonalClient {
    pub fn new(api_key: &str, config: TransportConfig) -> Result<Self, GarminError> {
        let base = Url::parse(GARMIN_ORIGIN).map_err(|_| GarminError::InvalidConfiguration)?;
        Self::with_base(api_key, base, config)
    }

    fn with_base(api_key: &str, base: Url, config: TransportConfig) -> Result<Self, GarminError> {
        let api_key = api_key.trim();
        if api_key.is_empty() {
            return Err(GarminError::InvalidConfiguration);
        }
        let authority =
            sensitive_header("ApiKey", api_key).map_err(|_| GarminError::InvalidConfiguration)?;
        let http = build_http(config, base.scheme() == "https")?
            .build()
            .map_err(|_| GarminError::InvalidConfiguration)?;
        Ok(Self {
            inner: Arc::new(Inner {
                http,
                base,
                api_key: authority,
                pacing: config.pacing,
                state: Mutex::new(Session {
                    token: None,
                    schedule: Schedule::new(),
                }),
            }),
        })
    }

    /// Return the original JSON bytes. Empty and unsupported results are distinct.
    pub async fn get_json(
        &self,
        request: PersonalRead,
        max_bytes: u64,
    ) -> Result<Option<Vec<u8>>, GarminError> {
        let url = request.url(&self.inner.base)?;
        let response = self.authorized_get(url, "application/json").await?;
        read_json_body(response, max_bytes).await
    }

    /// Write the original activity bytes to a caller-owned staging destination.
    /// The caller publishes its destination after successful completion.
    pub async fn download_activity(
        &self,
        id: NonZeroU64,
        max_bytes: u64,
        destination: &mut (impl AsyncWrite + Unpin),
    ) -> Result<u64, GarminError> {
        let url = self
            .inner
            .base
            .join(&format!("download-service/files/activity/{id}"))
            .map_err(|_| GarminError::InvalidRequest)?;
        let response = self
            .authorized_get(url, "application/x-zip-compressed")
            .await?;
        stream_download(response, max_bytes, destination).await
    }

    async fn authorized_get(
        &self,
        url: Url,
        accept: &'static str,
    ) -> Result<Response, GarminError> {
        let mut session = self.inner.state.lock().await;
        let mut refreshed = false;
        loop {
            if session
                .token
                .as_ref()
                .is_none_or(|token| token.renew_at <= Instant::now())
            {
                session.token = None;
                let token = self.fetch_token(&mut session).await?;
                session.token = Some(token);
                refreshed = true;
            }
            let authorization = session
                .token
                .as_ref()
                .ok_or(GarminError::InvalidTokenResponse)?
                .authorization
                .clone();
            let request = self
                .inner
                .http
                .get(url.clone())
                .header(AUTHORIZATION, authorization)
                .header(ACCEPT, accept);
            let response = session.schedule.send(self.inner.pacing, request).await?;
            if response.status() == StatusCode::UNAUTHORIZED {
                session.token = None;
                if !refreshed {
                    continue;
                }
            }
            return successful(response);
        }
    }

    async fn fetch_token(&self, session: &mut Session) -> Result<Token, GarminError> {
        let url = self
            .inner
            .base
            .join(TOKEN_PATH)
            .map_err(|_| GarminError::InvalidConfiguration)?;
        let request = self
            .inner
            .http
            .post(url)
            .header(AUTHORIZATION, self.inner.api_key.clone())
            .header(ACCEPT, "*/*");
        let response = successful(session.schedule.send(self.inner.pacing, request).await?)?;
        let body = read_bounded(response, MAX_TOKEN_BYTES).await?;
        token_from_response(&body)
    }
}

impl Schedule {
    fn new() -> Self {
        Self {
            next_request: Instant::now(),
            throttle: None,
        }
    }

    fn cooldown(&mut self, duration: Duration) {
        self.throttle = Some((Instant::now(), duration));
    }

    fn check(&mut self) -> Result<(), GarminError> {
        if let Some((since, duration)) = self.throttle {
            let retry_after = duration.saturating_sub(since.elapsed());
            if !retry_after.is_zero() {
                return Err(GarminError::RateLimited { retry_after });
            }
            self.throttle = None;
        }
        Ok(())
    }

    async fn send(
        &mut self,
        pacing: Duration,
        request: RequestBuilder,
    ) -> Result<Response, GarminError> {
        self.check()?;
        sleep_until(self.next_request).await;
        self.next_request = Instant::now()
            .checked_add(pacing)
            .ok_or(GarminError::InvalidConfiguration)?;
        let response = send(request).await?;
        if response.status() == StatusCode::TOO_MANY_REQUESTS {
            let retry_after = retry_delay(&response);
            self.cooldown(retry_after);
            return Err(GarminError::RateLimited { retry_after });
        }
        Ok(response)
    }
}

fn build_http(config: TransportConfig, https_only: bool) -> Result<ClientBuilder, GarminError> {
    if config.connect_timeout.is_zero()
        || config.read_timeout.is_zero()
        || config.total_timeout.is_zero()
    {
        return Err(GarminError::InvalidConfiguration);
    }
    let mut roots = rustls::RootCertStore::empty();
    roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
    if roots.is_empty() {
        return Err(GarminError::InvalidConfiguration);
    }
    let tls = rustls::ClientConfig::builder_with_provider(
        rustls::crypto::ring::default_provider().into(),
    )
    .with_safe_default_protocol_versions()
    .map_err(|_| GarminError::InvalidConfiguration)?
    .with_root_certificates(roots)
    .with_no_client_auth();
    Ok(Client::builder()
        .tls_backend_preconfigured(tls)
        .https_only(https_only)
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .no_proxy()
        .connect_timeout(config.connect_timeout)
        .read_timeout(config.read_timeout)
        .timeout(config.total_timeout))
}

fn sensitive_header(scheme: &str, value: &str) -> Result<HeaderValue, GarminError> {
    let mut header = HeaderValue::from_str(&format!("{scheme} {value}"))
        .map_err(|_| GarminError::InvalidTokenResponse)?;
    header.set_sensitive(true);
    Ok(header)
}

async fn send(request: RequestBuilder) -> Result<Response, GarminError> {
    request.send().await.map_err(|error| {
        if error.is_timeout() {
            GarminError::Timeout
        } else {
            GarminError::Network
        }
    })
}

fn token_from_response(body: &[u8]) -> Result<Token, GarminError> {
    let value = std::str::from_utf8(body)
        .map_err(|_| GarminError::InvalidTokenResponse)?
        .trim();
    let sections: Vec<_> = value.splitn(4, '.').collect();
    let [_, data, _] = sections.as_slice() else {
        return Err(GarminError::InvalidTokenResponse);
    };
    if sections.iter().any(|section| {
        section.is_empty()
            || !section
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    }) {
        return Err(GarminError::InvalidTokenResponse);
    }
    let data = URL_SAFE_NO_PAD
        .decode(data)
        .map_err(|_| GarminError::InvalidTokenResponse)?;
    let claims: Claims =
        serde_json::from_slice(&data).map_err(|_| GarminError::InvalidTokenResponse)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| GarminError::InvalidTokenResponse)?
        .as_secs();
    let ttl = claims
        .exp
        .checked_sub(now)
        .filter(|seconds| *seconds > 0)
        .ok_or(GarminError::InvalidTokenResponse)?
        .min(86_400);
    let margin = (ttl / 10).min(30);
    let authorization = sensitive_header("Bearer", value)?;
    Ok(Token {
        authorization,
        renew_at: Instant::now() + Duration::from_secs(ttl - margin),
    })
}

fn retry_delay(response: &Response) -> Duration {
    response
        .headers()
        .get(RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(parse_retry_after)
        .unwrap_or(Duration::from_mins(15))
}

fn parse_retry_after(value: &str) -> Option<Duration> {
    value
        .parse::<u64>()
        .ok()
        .map(Duration::from_secs)
        .or_else(|| {
            httpdate::parse_http_date(value)
                .ok()
                .map(|date| date.duration_since(SystemTime::now()).unwrap_or_default())
        })
}

fn successful(response: Response) -> Result<Response, GarminError> {
    match response.status() {
        StatusCode::UNAUTHORIZED => Err(GarminError::Unauthorized),
        StatusCode::FORBIDDEN => Err(GarminError::Forbidden),
        StatusCode::TOO_MANY_REQUESTS => Err(GarminError::RateLimited {
            retry_after: retry_delay(&response),
        }),
        status if status.is_redirection() => Err(GarminError::Redirect),
        status if status.is_server_error() => Err(GarminError::Upstream(status.as_u16())),
        StatusCode::PARTIAL_CONTENT => Err(GarminError::InterruptedTransfer),
        status if status.is_success() => Ok(response),
        status => Err(GarminError::UnexpectedStatus(status.as_u16())),
    }
}

async fn read_json_body(
    response: Response,
    max_bytes: u64,
) -> Result<Option<Vec<u8>>, GarminError> {
    let bytes = read_bounded(response, max_bytes).await?;
    if bytes.iter().all(u8::is_ascii_whitespace) {
        return Ok(None);
    }
    serde_json::from_slice::<&serde_json::value::RawValue>(&bytes)
        .map_err(|_| GarminError::InvalidJson)?;
    Ok(Some(bytes))
}

async fn stream_download(
    response: Response,
    max_bytes: u64,
    destination: &mut (impl AsyncWrite + Unpin),
) -> Result<u64, GarminError> {
    let mut body = BoundedBody::new(response, max_bytes)?;
    while let Some(chunk) = body.next().await? {
        destination
            .write_all(&chunk)
            .await
            .map_err(|_| GarminError::DestinationWrite)?;
    }
    if body.total == 0 {
        return Err(GarminError::EmptyDownload);
    }
    Ok(body.total)
}

struct BoundedBody {
    response: Response,
    max: u64,
    expected: Option<u64>,
    total: u64,
}

impl BoundedBody {
    fn new(response: Response, max: u64) -> Result<Self, GarminError> {
        let expected = response.content_length();
        if expected.is_some_and(|declared| declared > max) {
            return Err(GarminError::ResponseTooLarge);
        }
        Ok(Self {
            response,
            max,
            expected,
            total: 0,
        })
    }

    async fn next(&mut self) -> Result<Option<bytes::Bytes>, GarminError> {
        let Some(chunk) = self.response.chunk().await.map_err(|error| {
            if error.is_timeout() {
                GarminError::Timeout
            } else {
                GarminError::InterruptedTransfer
            }
        })?
        else {
            if self.expected.is_some_and(|length| length != self.total) {
                return Err(GarminError::InterruptedTransfer);
            }
            return Ok(None);
        };
        self.total = self
            .total
            .checked_add(chunk.len() as u64)
            .filter(|count| *count <= self.max)
            .ok_or(GarminError::ResponseTooLarge)?;
        Ok(Some(chunk))
    }
}

async fn read_bounded(response: Response, limit: u64) -> Result<Vec<u8>, GarminError> {
    let mut body = BoundedBody::new(response, limit)?;
    let mut output = Vec::new();
    while let Some(chunk) = body.next().await? {
        output.extend_from_slice(&chunk);
    }
    Ok(output)
}
