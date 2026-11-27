use std::time::Duration;

use ga_domain::{
    model_request::GeminiModelRequest,
    model_response::{MAX_RESPONSE_BYTES, StructuredModelOutput},
};
use reqwest::{
    Client, Response, StatusCode,
    header::{ACCEPT_ENCODING, HeaderValue},
};
use serde_json::Value;

use super::{
    ModelTransportError, ModelUnavailability, credential_header, http_failure, network_failure,
    read_body, response_failure, response_retry, transport_client,
};

const GEMINI_URL: &str = "https://generativelanguage.googleapis.com/v1beta/interactions";
const MAX_ATTEMPT: Duration = Duration::from_secs(60);
const MIN_ATTEMPT: Duration = Duration::from_millis(1);
const MAX_ERROR_BYTES: usize = 32 * 1024;

pub struct GeminiTransport {
    client: Client,
    key: HeaderValue,
}

impl GeminiTransport {
    pub fn new(api_key: &str) -> Result<Self, ModelTransportError> {
        Self::connect(api_key, true)
    }

    pub(super) fn connect(api_key: &str, https_only: bool) -> Result<Self, ModelTransportError> {
        let key = credential_header(api_key, false)?;
        let client = transport_client(MAX_ATTEMPT, https_only)?;
        Ok(Self { client, key })
    }

    /// Send a single authorized interaction within the caller's remaining attempt budget.
    pub async fn send(
        &self,
        request: &GeminiModelRequest,
        timeout: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        self.send_to(request, timeout, GEMINI_URL).await
    }

    pub(super) async fn send_to(
        &self,
        request: &GeminiModelRequest,
        timeout: Duration,
        endpoint: &str,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        if !(MIN_ATTEMPT..=MAX_ATTEMPT).contains(&timeout) {
            return Err(ModelTransportError::InvalidConfiguration);
        }
        let response = self
            .client
            .post(endpoint)
            .timeout(timeout)
            .header("x-goog-api-key", self.key.clone())
            .header(ACCEPT_ENCODING, "identity")
            .json(request.body())
            .send()
            .await
            .map_err(network_failure)?;
        if !response.status().is_success() {
            let status = response.status();
            let retry = response_retry(&response);
            if !matches!(
                status,
                StatusCode::TOO_MANY_REQUESTS | StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN
            ) && invalid_api_key(response).await
            {
                return Err(ModelUnavailability::Authorization
                    .with_retry(retry, crate::ModelFailureDetail::HttpStatus(status)));
            }
            return Err(http_failure(status, retry));
        }
        let body = read_body(response, MAX_RESPONSE_BYTES).await?;
        request.decode_response(&body).map_err(response_failure)
    }
}

async fn invalid_api_key(response: Response) -> bool {
    let Ok(body) = read_body(response, MAX_ERROR_BYTES).await else {
        return false;
    };
    let Ok(value) = serde_json::from_slice::<Value>(&body) else {
        return false;
    };
    let envelope = value.get("error").unwrap_or(&value);
    envelope["details"].as_array().is_some_and(|items| {
        items
            .iter()
            .any(|detail| detail["reason"] == "API_KEY_INVALID")
    })
}
