use std::{error::Error, fmt};

mod gemini;

pub use gemini::{GeminiModelRequest, MAX_GEMINI_AUDIO_BYTES};

use serde_json::{Value, json};

use crate::{
    model_response::{
        ModelResponseError, StructuredModelOutput, StructuredProvider, StructuredResponseSchema,
    },
    model_schema::{ModelSchemaError, compile_model_schema},
};

/// Only application-owned contracts may become provider request metadata.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ApprovedModelSchema {
    AgentStep,
    Interpretation,
    SafetyScreen,
    TrackerExtraction,
    ModelProbe,
}

impl ApprovedModelSchema {
    pub(crate) fn document(self) -> &'static [u8] {
        match self {
            Self::AgentStep => include_bytes!("../contracts/model-schemas/agent-step.json"),
            Self::Interpretation => {
                include_bytes!("../contracts/model-schemas/interpretation.json")
            }
            Self::SafetyScreen => include_bytes!("../contracts/model-schemas/safety-screen.json"),
            Self::TrackerExtraction => {
                include_bytes!("../contracts/model-schemas/tracker-extraction.json")
            }
            Self::ModelProbe => include_bytes!("../contracts/model-schemas/model-probe.json"),
        }
    }
}

pub struct StructuredModelRequest {
    provider: StructuredProvider,
    body: Value,
    schema: StructuredResponseSchema,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelRequestError {
    InvalidConfiguration,
    InputTooLarge,
    Schema(ModelSchemaError),
    ResponseSchema(ModelResponseError),
}

impl fmt::Display for ModelRequestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfiguration => f.write_str("Model configuration is invalid"),
            Self::InputTooLarge => f.write_str("Model input exceeds the size limit"),
            Self::Schema(error) => error.fmt(f),
            Self::ResponseSchema(error) => error.fmt(f),
        }
    }
}
impl Error for ModelRequestError {}

impl StructuredModelRequest {
    pub fn prepare(
        provider: StructuredProvider,
        contract: ApprovedModelSchema,
        model: &str,
        max_output_tokens: u16,
        instruction: &str,
        prompt: &str,
    ) -> Result<Self, ModelRequestError> {
        if !(1..=200).contains(&model.chars().count()) || !(256..=8192).contains(&max_output_tokens)
        {
            return Err(ModelRequestError::InvalidConfiguration);
        }
        let original = contract.document();
        let application: Value = serde_json::from_slice(original)
            .map_err(|_| ModelRequestError::Schema(ModelSchemaError::InvalidDocument))?;
        let wire = compile_model_schema(provider, original).map_err(ModelRequestError::Schema)?;
        let schema = StructuredResponseSchema::new(&wire, &application)
            .map_err(ModelRequestError::ResponseSchema)?;
        let body = match provider {
            StructuredProvider::OpenAi => json!({
                "model": model,
                "store": false,
                "max_output_tokens": max_output_tokens,
                "input": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": prompt},
                ],
                "text": {"format": {
                    "type": "json_schema",
                    "name": "application_response",
                    "strict": true,
                    "schema": wire,
                }},
            }),
            StructuredProvider::Anthropic => json!({
                "model": model,
                "max_tokens": max_output_tokens,
                "system": instruction,
                "messages": [{"role": "user", "content": prompt}],
                "output_config": {"format": {"type": "json_schema", "schema": wire}},
            }),
        };
        Ok(Self {
            provider,
            body,
            schema,
        })
    }

    pub fn provider(&self) -> StructuredProvider {
        self.provider
    }

    pub fn body(&self) -> &Value {
        &self.body
    }

    /// Validate the response with the exact contracts used to prepare this request.
    pub fn decode_response(
        &self,
        response: &[u8],
    ) -> Result<StructuredModelOutput, ModelResponseError> {
        self.schema.decode(self.provider, response)
    }
}
