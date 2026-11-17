mod poller;
pub use poller::{PollBatch, PollProgress, TelegramLoopError};

use std::{error::Error, fmt, time::Duration};

use reqwest::{
    Client, Response, StatusCode,
    header::{ACCEPT_ENCODING, CONTENT_ENCODING, RETRY_AFTER},
};
use serde_json::{Value, json};

const TELEGRAM_API: &str = "https://api.telegram.org";
const POLL_SECONDS: u64 = 15;
const POLL_DEADLINE: Duration = Duration::from_secs(POLL_SECONDS + 10);
const ACK_DEADLINE: Duration = Duration::from_secs(5);
const MAX_UPDATE_BYTES: usize = 8 * 1024 * 1024;
const MAX_ACK_BYTES: usize = 64 * 1024;
const MAX_UPDATES: usize = 100;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelegramTransportError {
    Configuration,
    InvalidInput,
    Authorization,
    RateLimited(Duration),
    Rejected,
    Unavailable,
    InvalidResponse,
    ResponseTooLarge,
}

impl fmt::Display for TelegramTransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Configuration => "Telegram transport configuration is invalid",
            Self::InvalidInput => "Telegram request parameters are invalid",
            Self::Authorization => "Telegram authorization failed",
            Self::RateLimited(_) => "Telegram request is rate limited",
            Self::Rejected => "Telegram rejected the request",
            Self::Unavailable => "Telegram request did not complete",
            Self::InvalidResponse => "Telegram returned an invalid response",
            Self::ResponseTooLarge => "Telegram response exceeds the size limit",
        })
    }
}
impl Error for TelegramTransportError {}

/// Verified provider JSON. The channel adapter authorizes its private sender separately.
pub struct TelegramUpdate {
    pub id: i64,
    pub body: Value,
}

/// The long poll and callback acknowledgements have independent connection pools.
pub struct TelegramTransport {
    base: String,
    polling: Client,
    acknowledgements: Client,
}

impl TelegramTransport {
    pub fn new(token: &str) -> Result<Self, TelegramTransportError> {
        Self::connect(token, TELEGRAM_API, true)
    }

    fn connect(
        token: &str,
        origin: &str,
        https_only: bool,
    ) -> Result<Self, TelegramTransportError> {
        let valid = token.split_once(':').is_some_and(|(id, key)| {
            !id.is_empty()
                && id.bytes().all(|byte| byte.is_ascii_digit())
                && !key.is_empty()
                && key
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
        });
        if !valid || token.len() > 256 {
            return Err(TelegramTransportError::Configuration);
        }
        let mut roots = rustls::RootCertStore::empty();
        roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
        if roots.is_empty() {
            return Err(TelegramTransportError::Configuration);
        }
        let tls = rustls::ClientConfig::builder_with_provider(
            rustls::crypto::ring::default_provider().into(),
        )
        .with_safe_default_protocol_versions()
        .map_err(|_| TelegramTransportError::Configuration)?
        .with_root_certificates(roots)
        .with_no_client_auth();
        let build = |timeout| {
            Client::builder()
                .tls_backend_preconfigured(tls.clone())
                .https_only(https_only)
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .retry(reqwest::retry::never())
                .timeout(timeout)
                .build()
                .map_err(|_| TelegramTransportError::Configuration)
        };
        Ok(Self {
            base: format!("{origin}/bot{token}"),
            polling: build(POLL_DEADLINE)?,
            acknowledgements: build(ACK_DEADLINE)?,
        })
    }

    /// Poll using the latest durable offset. This call can acknowledge earlier updates.
    pub async fn get_updates(
        &self,
        offset: Option<i64>,
    ) -> Result<Vec<TelegramUpdate>, TelegramTransportError> {
        if offset.is_some_and(|offset| offset <= 0) {
            return Err(TelegramTransportError::InvalidInput);
        }
        let mut request = json!({
            "timeout": POLL_SECONDS,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        });
        if let Some(offset) = offset {
            request["offset"] = json!(offset);
        }
        let body = self
            .call(&self.polling, "getUpdates", &request, MAX_UPDATE_BYTES)
            .await?;
        let Value::Array(updates) = body else {
            return Err(TelegramTransportError::InvalidResponse);
        };
        if updates.len() > MAX_UPDATES {
            return Err(TelegramTransportError::InvalidResponse);
        }
        updates
            .into_iter()
            .map(|body| {
                let id = body["update_id"]
                    .as_i64()
                    .filter(|id| *id > 0)
                    .ok_or(TelegramTransportError::InvalidResponse)?;
                Ok(TelegramUpdate { id, body })
            })
            .collect()
    }

    pub async fn acknowledge_callback(
        &self,
        callback_id: &str,
    ) -> Result<(), TelegramTransportError> {
        if callback_id.is_empty() || callback_id.len() > 1000 || callback_id.contains('\0') {
            return Err(TelegramTransportError::InvalidInput);
        }
        let result = self
            .call(
                &self.acknowledgements,
                "answerCallbackQuery",
                &json!({"callback_query_id":callback_id}),
                MAX_ACK_BYTES,
            )
            .await?;
        if result == Value::Bool(true) {
            Ok(())
        } else {
            Err(TelegramTransportError::InvalidResponse)
        }
    }

    async fn call(
        &self,
        client: &Client,
        method: &'static str,
        request: &Value,
        limit: usize,
    ) -> Result<Value, TelegramTransportError> {
        let response = client
            .post(format!("{}/{method}", self.base))
            .header(ACCEPT_ENCODING, "identity")
            .json(request)
            .send()
            .await
            .map_err(|_| TelegramTransportError::Unavailable)?;
        let status = response.status();
        let retry = response
            .headers()
            .get(RETRY_AFTER)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse::<u64>().ok());
        if status == StatusCode::TOO_MANY_REQUESTS {
            let body = read_json(response, MAX_ACK_BYTES)
                .await
                .unwrap_or(Value::Null);
            return Err(rate_limited(&body, retry));
        }
        if !status.is_success() {
            return Err(failure(u64::from(status.as_u16()), &Value::Null, retry));
        }
        let mut body = read_json(response, limit).await?;
        match body["ok"].as_bool() {
            Some(true) if body.get("result").is_some() => Ok(body["result"].take()),
            Some(false) => Err(failure(
                body["error_code"].as_u64().unwrap_or(0),
                &body,
                retry,
            )),
            _ => Err(TelegramTransportError::InvalidResponse),
        }
    }
}

fn failure(code: u64, body: &Value, retry: Option<u64>) -> TelegramTransportError {
    match code {
        401 | 403 => TelegramTransportError::Authorization,
        429 => rate_limited(body, retry),
        408 | 500..=599 => TelegramTransportError::Unavailable,
        _ => TelegramTransportError::Rejected,
    }
}

fn rate_limited(body: &Value, retry: Option<u64>) -> TelegramTransportError {
    let delay = retry
        .unwrap_or(0)
        .max(body["parameters"]["retry_after"].as_u64().unwrap_or(0))
        .max(1);
    TelegramTransportError::RateLimited(Duration::from_secs(delay))
}

async fn read_json(mut response: Response, limit: usize) -> Result<Value, TelegramTransportError> {
    if response
        .headers()
        .get_all(CONTENT_ENCODING)
        .iter()
        .any(|value| !value.as_bytes().eq_ignore_ascii_case(b"identity"))
    {
        return Err(TelegramTransportError::InvalidResponse);
    }
    if response
        .content_length()
        .is_some_and(|bytes| bytes > limit as u64)
    {
        return Err(TelegramTransportError::ResponseTooLarge);
    }
    let mut body = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| TelegramTransportError::Unavailable)?
    {
        if chunk.len() > limit.saturating_sub(body.len()) {
            return Err(TelegramTransportError::ResponseTooLarge);
        }
        body.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&body).map_err(|_| TelegramTransportError::InvalidResponse)
}

#[cfg(test)]
mod tests;
