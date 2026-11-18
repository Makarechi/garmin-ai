mod local_text;
mod model_reply;
mod model_worker;
mod poller;
mod reminders;
mod send;
mod worker;
pub use model_reply::{DiaryReplyOutcome, DiaryReplyPreferences, publish_telegram_interpretation};
pub use poller::{PollBatch, PollProgress, TelegramLoopError};
pub use reminders::DiaryReminderSettings;
pub use send::TelegramSendOutcome;

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
const SEND_DEADLINE: Duration = Duration::from_secs(10);
const MAX_UPDATE_BYTES: usize = 8 * 1024 * 1024;
const MAX_ACK_BYTES: usize = 64 * 1024;
const MAX_UPDATES: usize = 100;
const VOICE_DEADLINE: Duration = Duration::from_secs(30);
const MAX_VOICE_BYTES: usize = 20 * 1024 * 1024;
const MAX_VOICE_SECONDS: u64 = 600;

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

/// Polling, confirmations, delivery and downloads use independent connection pools.
pub struct TelegramTransport {
    base: String,
    polling: Client,
    acknowledgements: Client,
    delivery: Client,
    downloads: Client,
    file_base: String,
}

impl TelegramTransport {
    pub fn new(token: &str) -> Result<Self, TelegramTransportError> {
        Self::connect(token, TELEGRAM_API, true)
    }

    /// Read the webhook backlog, or report that the bot uses polling.
    async fn webhook_backlog(&self) -> Result<Option<u64>, TelegramTransportError> {
        let value = self
            .call(&self.polling, "getWebhookInfo", &json!({}), MAX_ACK_BYTES)
            .await?;
        match value["url"].as_str() {
            Some("") => Ok(None),
            Some(_) => value["pending_update_count"]
                .as_u64()
                .map(Some)
                .ok_or(TelegramTransportError::InvalidResponse),
            None => Err(TelegramTransportError::InvalidResponse),
        }
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
            delivery: build(SEND_DEADLINE)?,
            downloads: build(VOICE_DEADLINE)?,
            file_base: format!("{origin}/file/bot{token}"),
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

    /// Download a voice attachment only from the fixed Bot API file origin.
    /// The caller supplies metadata from an authenticated private message.
    pub async fn download_voice(
        &self,
        file_id: &str,
        duration_seconds: Option<u64>,
        declared_bytes: Option<u64>,
    ) -> Result<Vec<u8>, TelegramTransportError> {
        if file_id.is_empty() || file_id.len() > 500 || file_id.chars().any(char::is_control) {
            return Err(TelegramTransportError::InvalidInput);
        }
        if duration_seconds.is_some_and(|seconds| seconds > MAX_VOICE_SECONDS)
            || declared_bytes.is_some_and(|bytes| bytes > MAX_VOICE_BYTES as u64)
        {
            return Err(TelegramTransportError::ResponseTooLarge);
        }
        tokio::time::timeout(VOICE_DEADLINE, self.voice_file(file_id))
            .await
            .map_err(|_| TelegramTransportError::Unavailable)?
    }

    async fn voice_file(&self, file_id: &str) -> Result<Vec<u8>, TelegramTransportError> {
        let metadata = self
            .call(
                &self.downloads,
                "getFile",
                &json!({"file_id":file_id}),
                MAX_ACK_BYTES,
            )
            .await?;
        let file_path = metadata["file_path"]
            .as_str()
            .filter(|path| valid_file_path(path))
            .ok_or(TelegramTransportError::InvalidResponse)?;
        let metadata_size = &metadata["file_size"];
        if !metadata_size.is_null() {
            let bytes = metadata_size
                .as_u64()
                .ok_or(TelegramTransportError::InvalidResponse)?;
            if bytes > MAX_VOICE_BYTES as u64 {
                return Err(TelegramTransportError::ResponseTooLarge);
            }
        }
        let response = self
            .downloads
            .get(format!("{}/{file_path}", self.file_base))
            .header(ACCEPT_ENCODING, "identity")
            .send()
            .await
            .map_err(|_| TelegramTransportError::Unavailable)?;
        let status = response.status();
        if status != StatusCode::OK {
            return Err(failure(
                u64::from(status.as_u16()),
                &Value::Null,
                retry_after(&response),
            ));
        }
        let data = read_body(response, MAX_VOICE_BYTES).await?;
        if data.is_empty() {
            return Err(TelegramTransportError::InvalidResponse);
        }
        Ok(data)
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
        let retry = retry_after(&response);
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

fn valid_file_path(path: &str) -> bool {
    !path.is_empty()
        && path.len() <= 1024
        && path.split('/').all(|part| {
            !matches!(part, "" | "." | "..")
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
        })
}

fn retry_after(response: &Response) -> Option<u64> {
    response
        .headers()
        .get(RETRY_AFTER)?
        .to_str()
        .ok()?
        .parse()
        .ok()
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

async fn read_json(response: Response, limit: usize) -> Result<Value, TelegramTransportError> {
    let body = read_body(response, limit).await?;
    serde_json::from_slice(&body).map_err(|_| TelegramTransportError::InvalidResponse)
}

async fn read_body(
    mut response: Response,
    limit: usize,
) -> Result<Vec<u8>, TelegramTransportError> {
    if response
        .headers()
        .get_all(CONTENT_ENCODING)
        .iter()
        .any(|value| !value.as_bytes().eq_ignore_ascii_case(b"identity"))
    {
        return Err(TelegramTransportError::InvalidResponse);
    }
    let length = response.content_length().unwrap_or_default();
    if length > limit as u64 {
        return Err(TelegramTransportError::ResponseTooLarge);
    }
    let mut body = Vec::with_capacity(length as usize);
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| TelegramTransportError::Unavailable)?
    {
        if chunk.len() > limit - body.len() {
            return Err(TelegramTransportError::ResponseTooLarge);
        }
        // An undeclared length must not trigger Vec's exponential growth above the limit.
        body.reserve_exact(chunk.len());
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

#[cfg(test)]
mod tests;
