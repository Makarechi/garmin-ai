mod encoding;

use std::{error::Error, fmt};

use jsonschema::Validator;
use serde_json::Value;

pub const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const MAX_USAGE_TOKENS: u64 = 1_000_000_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StructuredProvider {
    OpenAi,
    Anthropic,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ModelUsage {
    pub input_tokens: Option<u32>,
    pub output_tokens: Option<u32>,
}

pub struct StructuredModelOutput {
    pub value: Value,
    pub usage: Option<ModelUsage>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelResponseError {
    InvalidSchema,
    TooLarge,
    InvalidEnvelope,
    InvalidOutput,
    Incomplete,
    Refused,
    Unavailable,
}

impl fmt::Display for ModelResponseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidSchema => "Model response schema could not be verified",
            Self::TooLarge => "Model response exceeds the size limit",
            Self::InvalidEnvelope => "Model response has an invalid envelope",
            Self::InvalidOutput => "Model output failed validation",
            Self::Incomplete => "Model response was incomplete",
            Self::Refused => "Model refused the request",
            Self::Unavailable => "Model response did not complete",
        })
    }
}
impl Error for ModelResponseError {}

/// Compile the provider wire contract and the full local application contract.
pub struct StructuredResponseSchema {
    wire: Validator,
    application: Validator,
}

impl StructuredResponseSchema {
    pub fn new(wire: &Value, application: &Value) -> Result<Self, ModelResponseError> {
        let wire = jsonschema::draft202012::options()
            .should_validate_formats(false)
            .build(wire)
            .map_err(|_| ModelResponseError::InvalidSchema)?;
        let application = jsonschema::draft202012::options()
            .should_validate_formats(true)
            .with_format("uuid", |text| uuid::Uuid::parse_str(text).is_ok())
            .with_format("date-time", |text| {
                crate::event_time::parse_aware_datetime(&Value::String(text.to_owned())).is_some()
            })
            .build(application)
            .map_err(|_| ModelResponseError::InvalidSchema)?;
        Ok(Self { wire, application })
    }

    /// Decode a completed structured response without carrying provider errors or prompt text.
    pub fn decode(
        &self,
        provider: StructuredProvider,
        body: &[u8],
    ) -> Result<StructuredModelOutput, ModelResponseError> {
        if body.len() > MAX_RESPONSE_BYTES {
            return Err(ModelResponseError::TooLarge);
        }
        let document = encoding::decode_text(body).ok_or(ModelResponseError::InvalidEnvelope)?;
        let envelope: Value =
            serde_json::from_str(&document).map_err(|_| ModelResponseError::InvalidEnvelope)?;
        if !envelope.is_object() {
            return Err(ModelResponseError::InvalidEnvelope);
        }
        let text = match provider {
            StructuredProvider::OpenAi => openai_text(&envelope)?,
            StructuredProvider::Anthropic => anthropic_text(&envelope)?,
        };
        let value: Value =
            serde_json::from_str(text).map_err(|_| ModelResponseError::InvalidOutput)?;
        if !self.wire.is_valid(&value) || !self.application.is_valid(&value) {
            return Err(ModelResponseError::InvalidOutput);
        }
        let usage = if envelope["usage"].is_object() {
            let input_tokens = tokens(&envelope["usage"]["input_tokens"])?;
            let output_tokens = tokens(&envelope["usage"]["output_tokens"])?;
            Some(ModelUsage {
                input_tokens,
                output_tokens,
            })
        } else {
            None
        };
        Ok(StructuredModelOutput { value, usage })
    }
}

fn openai_text(envelope: &Value) -> Result<&str, ModelResponseError> {
    match envelope["status"].as_str() {
        Some("completed") => {}
        Some("incomplete") => return Err(ModelResponseError::Incomplete),
        _ => return Err(ModelResponseError::Unavailable),
    }
    let output = envelope["output"]
        .as_array()
        .ok_or(ModelResponseError::InvalidOutput)?;
    let mut text = None;
    for item in output {
        match item["type"].as_str() {
            Some("reasoning") => continue,
            Some("message") => {}
            _ => return Err(ModelResponseError::InvalidOutput),
        }
        let content = item["content"]
            .as_array()
            .ok_or(ModelResponseError::InvalidOutput)?;
        for block in content {
            match block["type"].as_str() {
                Some("refusal") => return Err(ModelResponseError::Refused),
                Some("output_text") => {
                    let value = block["text"]
                        .as_str()
                        .ok_or(ModelResponseError::InvalidOutput)?;
                    text = Some((value, text.is_some()));
                }
                _ => return Err(ModelResponseError::InvalidOutput),
            }
        }
    }
    single_text(text)
}

fn anthropic_text(envelope: &Value) -> Result<&str, ModelResponseError> {
    let reason = envelope["stop_reason"].as_str();
    if reason == Some("refusal") || envelope["stop_details"]["type"] == "refusal" {
        return Err(ModelResponseError::Refused);
    }
    match reason {
        Some("end_turn") => {}
        Some("max_tokens") => return Err(ModelResponseError::Incomplete),
        _ => return Err(ModelResponseError::InvalidOutput),
    }
    let content = envelope["content"]
        .as_array()
        .ok_or(ModelResponseError::InvalidOutput)?;
    let mut text = None;
    for block in content {
        match block["type"].as_str() {
            Some("thinking" | "redacted_thinking") => continue,
            Some("text") => {
                let value = block["text"]
                    .as_str()
                    .ok_or(ModelResponseError::InvalidOutput)?;
                text = Some((value, text.is_some()));
            }
            _ => return Err(ModelResponseError::InvalidOutput),
        }
    }
    single_text(text)
}

fn single_text(text: Option<(&str, bool)>) -> Result<&str, ModelResponseError> {
    match text {
        Some((text, false)) => Ok(text),
        _ => Err(ModelResponseError::InvalidOutput),
    }
}

fn tokens(value: &Value) -> Result<Option<u32>, ModelResponseError> {
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_u64()
        .filter(|count| *count <= MAX_USAGE_TOKENS)
        .map(|count| Some(count as u32))
        .ok_or(ModelResponseError::InvalidOutput)
}
