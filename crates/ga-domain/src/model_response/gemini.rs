use jsonschema::Validator;
use serde_json::Value;

use super::{
    ModelResponseError, ModelUsage, StructuredModelOutput, application_validator, decode_envelope,
    tokens,
};

/// Gemini output is evaluated locally against the application's original schema.
/// The narrower wire schema instructs the model and can require optional application fields.
pub struct GeminiResponseSchema {
    application: Validator,
}

impl GeminiResponseSchema {
    pub fn new(application: &Value) -> Result<Self, ModelResponseError> {
        Ok(Self {
            application: application_validator(application)?,
        })
    }

    pub fn decode(&self, body: &[u8]) -> Result<StructuredModelOutput, ModelResponseError> {
        let envelope = decode_envelope(body)?;
        match envelope["status"].as_str() {
            Some("completed") => {}
            Some("incomplete" | "budget_exceeded") => return Err(ModelResponseError::Incomplete),
            Some("failed" | "cancelled") => return Err(ModelResponseError::Unavailable),
            _ => return Err(ModelResponseError::InvalidOutput),
        }
        let text = last_output_text(&envelope);
        let value: Value =
            serde_json::from_str(&text).map_err(|_| ModelResponseError::InvalidOutput)?;
        if !self.application.is_valid(&value) {
            return Err(ModelResponseError::InvalidOutput);
        }
        let usage = envelope["usage"].is_object().then(|| ModelUsage {
            input_tokens: tokens(&envelope["usage"]["total_input_tokens"])
                .ok()
                .flatten(),
            output_tokens: tokens(&envelope["usage"]["total_output_tokens"])
                .ok()
                .flatten(),
        });
        Ok(StructuredModelOutput { value, usage })
    }
}

fn last_output_text(envelope: &Value) -> String {
    let mut parts = Vec::new();
    let steps = envelope["steps"].as_array().map_or(&[][..], Vec::as_slice);
    // The SDK builds output_text from the trailing text of the last uninterrupted output.
    // Earlier turns, thought blocks, or function calls may not supply the application result.
    'steps: for step in steps.iter().rev() {
        match step["type"].as_str() {
            Some("user_input") => break,
            Some("model_output") => {}
            _ if parts.is_empty() => continue,
            _ => break,
        }
        let Some(content) = step["content"].as_array() else {
            if parts.is_empty() {
                continue;
            }
            break;
        };
        for block in content.iter().rev() {
            if block["type"] == "text" {
                parts.push(block["text"].as_str().unwrap_or_default());
            } else if !parts.is_empty() {
                break 'steps;
            }
        }
    }
    parts.into_iter().rev().collect()
}
