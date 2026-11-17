pub mod fallback;
mod gemini;

pub use gemini::GeminiTransport;

use std::{
    error::Error,
    fmt,
    time::{Duration, SystemTime},
};

use ga_domain::{
    model_request::StructuredModelRequest,
    model_response::{
        MAX_RESPONSE_BYTES, ModelResponseError, StructuredModelOutput, StructuredProvider,
    },
};
use reqwest::{
    Client, Response, StatusCode,
    header::{
        ACCEPT_ENCODING, AUTHORIZATION, CONTENT_ENCODING, HeaderName, HeaderValue, RETRY_AFTER,
    },
};

const OPENAI_URL: &str = "https://api.openai.com/v1/responses";
const ANTHROPIC_URL: &str = "https://api.anthropic.com/v1/messages";
const ANTHROPIC_VERSION: &str = "2026-10-21";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelUnavailability {
    Authorization,
    MissingModel,
    RateLimited,
    Upstream,
}

impl ModelUnavailability {
    fn default_retry(self) -> Duration {
        Duration::from_secs(match self {
            Self::Authorization | Self::MissingModel => 1800,
            Self::RateLimited => 120,
            Self::Upstream => 60,
        })
    }

    fn with_retry(self, retry: Option<Duration>) -> ModelTransportError {
        ModelTransportError::Unavailable {
            reason: self,
            retry_after: self.default_retry().max(retry.unwrap_or_default()),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelTransportError {
    InvalidConfiguration,
    RequestRejected,
    Unavailable {
        reason: ModelUnavailability,
        retry_after: Duration,
    },
    Response(ModelResponseError),
}

impl fmt::Display for ModelTransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::InvalidConfiguration => "Model transport configuration is invalid",
            Self::RequestRejected => "Model request was rejected",
            Self::Unavailable { reason, .. } => match reason {
                ModelUnavailability::Authorization => "Model authorization failed",
                ModelUnavailability::MissingModel => "Configured model is unavailable",
                ModelUnavailability::RateLimited => "Model request is rate limited",
                ModelUnavailability::Upstream => "Model request failed",
            },
            Self::Response(error) => return error.fmt(f),
        };
        f.write_str(message)
    }
}
impl Error for ModelTransportError {}

pub struct ModelTransport {
    client: Client,
    provider: StructuredProvider,
    authorization: HeaderValue,
}

impl ModelTransport {
    pub fn new(
        provider: StructuredProvider,
        api_key: &str,
        timeout: Duration,
    ) -> Result<Self, ModelTransportError> {
        Self::connect(provider, api_key, timeout, true)
    }

    fn connect(
        provider: StructuredProvider,
        api_key: &str,
        timeout: Duration,
        https_only: bool,
    ) -> Result<Self, ModelTransportError> {
        if !(Duration::from_secs(1)..=Duration::from_secs(120)).contains(&timeout) {
            return Err(ModelTransportError::InvalidConfiguration);
        }
        let authorization = credential_header(api_key, true)?;
        let client = transport_client(timeout, https_only)?;
        Ok(Self {
            client,
            provider,
            authorization,
        })
    }

    /// Send one already authorized application request to this provider.
    pub async fn send(
        &self,
        request: &StructuredModelRequest,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        let endpoint = match self.provider {
            StructuredProvider::OpenAi => OPENAI_URL,
            StructuredProvider::Anthropic => ANTHROPIC_URL,
        };
        self.send_to(request, endpoint).await
    }

    async fn send_to(
        &self,
        request: &StructuredModelRequest,
        endpoint: &str,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        if request.provider() != self.provider {
            return Err(ModelTransportError::InvalidConfiguration);
        }
        let mut http = self
            .client
            .post(endpoint)
            .header(AUTHORIZATION, self.authorization.clone())
            .header(ACCEPT_ENCODING, "identity")
            .json(request.body());
        if self.provider == StructuredProvider::Anthropic {
            http = http.header(
                HeaderName::from_static("anthropic-version"),
                ANTHROPIC_VERSION,
            );
        }
        let response = http.send().await.map_err(network_failure)?;
        if !response.status().is_success() {
            return Err(http_failure(response.status(), response_retry(&response)));
        }
        let body = read_body(response, MAX_RESPONSE_BYTES).await?;
        request.decode_response(&body).map_err(response_failure)
    }
}

fn credential_header(api_key: &str, bearer: bool) -> Result<HeaderValue, ModelTransportError> {
    let invalid = ModelTransportError::InvalidConfiguration;
    if api_key.is_empty() || api_key.bytes().any(|byte| !(0x21..=0x7e).contains(&byte)) {
        return Err(invalid);
    }
    let mut header = if bearer {
        HeaderValue::from_str(&format!("Bearer {api_key}"))
    } else {
        HeaderValue::from_str(api_key)
    }
    .map_err(|_| invalid)?;
    header.set_sensitive(true);
    Ok(header)
}

fn transport_client(timeout: Duration, https_only: bool) -> Result<Client, ModelTransportError> {
    let invalid = ModelTransportError::InvalidConfiguration;
    let mut roots = rustls::RootCertStore::empty();
    roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
    if roots.is_empty() {
        return Err(invalid);
    }
    let tls = rustls::ClientConfig::builder_with_provider(
        rustls::crypto::ring::default_provider().into(),
    )
    .with_safe_default_protocol_versions()
    .map_err(|_| invalid)?
    .with_root_certificates(roots)
    .with_no_client_auth();
    Client::builder()
        .tls_backend_preconfigured(tls)
        .https_only(https_only)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .timeout(timeout)
        .build()
        .map_err(|_| invalid)
}

fn network_failure(_: reqwest::Error) -> ModelTransportError {
    ModelUnavailability::Upstream.with_retry(None)
}

fn response_failure(error: ModelResponseError) -> ModelTransportError {
    match error {
        ModelResponseError::Unavailable => ModelUnavailability::Upstream.with_retry(None),
        other => ModelTransportError::Response(other),
    }
}

fn response_retry(response: &Response) -> Option<Duration> {
    response
        .headers()
        .get(RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| retry_delay(value, SystemTime::now()))
}

async fn read_body(mut response: Response, limit: usize) -> Result<Vec<u8>, ModelTransportError> {
    if response
        .headers()
        .get_all(CONTENT_ENCODING)
        .iter()
        .any(|encoding| !encoding.as_bytes().eq_ignore_ascii_case(b"identity"))
    {
        return Err(ModelTransportError::Response(
            ModelResponseError::InvalidEnvelope,
        ));
    }
    if response
        .content_length()
        .is_some_and(|length| length > limit as u64)
    {
        return Err(ModelTransportError::Response(ModelResponseError::TooLarge));
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(network_failure)? {
        if chunk.len() > limit - body.len() {
            return Err(ModelTransportError::Response(ModelResponseError::TooLarge));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn http_failure(status: StatusCode, retry: Option<Duration>) -> ModelTransportError {
    let reason = match status {
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => ModelUnavailability::Authorization,
        StatusCode::NOT_FOUND => ModelUnavailability::MissingModel,
        StatusCode::TOO_MANY_REQUESTS => ModelUnavailability::RateLimited,
        StatusCode::REQUEST_TIMEOUT => ModelUnavailability::Upstream,
        status if status.as_u16() >= 500 => ModelUnavailability::Upstream,
        _ => return ModelTransportError::RequestRejected,
    };
    reason.with_retry(retry)
}

fn retry_delay(header: &str, now: SystemTime) -> Option<Duration> {
    let seconds = match header.trim().parse::<f64>() {
        Ok(seconds) => seconds,
        Err(_) => httpdate::parse_http_date(header.trim())
            .ok()?
            .duration_since(now)
            .ok()?
            .as_secs_f64(),
    };
    // The persisted gate uses a UTC datetime with a maximum year of 9999.
    let latest = SystemTime::UNIX_EPOCH + Duration::from_secs(253_402_300_799);
    let delay = Duration::try_from_secs_f64(seconds.ceil()).ok()?;
    (!delay.is_zero() && now.checked_add(delay)? <= latest).then_some(delay)
}

#[cfg(test)]
mod tests;
