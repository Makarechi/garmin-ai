use std::{error::Error, fmt, sync::OnceLock};

mod gemini;

pub use gemini::{GeminiModelRequest, MAX_GEMINI_AUDIO_BYTES};

use serde_json::{Value, json};

use crate::{
    model_response::{
        ExtensionResponseSchema, ModelResponseError, StructuredModelOutput, StructuredProvider,
        StructuredResponseSchema,
    },
    model_schema::{ModelSchemaError, compile_model_schema},
};

/// Only application-owned contracts may become provider request metadata.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ApprovedModelSchema {
    AgentStep,
    Interpretation,
    ConversationInterpretation,
    SafetyScreen,
    TrackerExtraction,
    ModelProbe,
}

impl ApprovedModelSchema {
    pub(crate) fn document(self) -> Result<&'static [u8], ModelSchemaError> {
        Ok(match self {
            Self::AgentStep => include_bytes!("../contracts/model-schemas/agent-step.json"),
            Self::Interpretation => INTERPRETATION,
            Self::ConversationInterpretation => conversation_interpretation()?,
            Self::SafetyScreen => include_bytes!("../contracts/model-schemas/safety-screen.json"),
            Self::TrackerExtraction => {
                include_bytes!("../contracts/model-schemas/tracker-extraction.json")
            }
            Self::ModelProbe => include_bytes!("../contracts/model-schemas/model-probe.json"),
        })
    }
}

const INTERPRETATION: &[u8] = include_bytes!("../contracts/model-schemas/interpretation.json");

fn conversation_interpretation() -> Result<&'static [u8], ModelSchemaError> {
    static DOCUMENT: OnceLock<Option<Vec<u8>>> = OnceLock::new();
    DOCUMENT
        .get_or_init(|| {
            let mut schema: Value = serde_json::from_slice(INTERPRETATION).ok()?;
            schema["properties"].as_object_mut()?.insert(
                "capability_answer".into(),
                json!({"anyOf":[{"type":"string","minLength":1,"maxLength":900},{"type":"null"}],
                       "description":"An answer about the assistant's documented features only, in the response language."}),
            );
            serde_json::to_vec(&schema).ok()
        })
        .as_deref()
        .ok_or(ModelSchemaError::InvalidDocument)
}

pub struct StructuredModelRequest {
    provider: StructuredProvider,
    body: Value,
    schema: StructuredResponseSchema,
}

pub struct ExtensionModelRequest {
    params: Value,
    schema: ExtensionResponseSchema,
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

impl ExtensionModelRequest {
    pub fn prepare(
        contract: ApprovedModelSchema,
        model: Option<&str>,
        instruction: &str,
        prompt: &str,
    ) -> Result<Self, ModelRequestError> {
        let application: Value =
            serde_json::from_slice(contract.document().map_err(ModelRequestError::Schema)?)
                .map_err(|_| ModelRequestError::Schema(ModelSchemaError::InvalidDocument))?;
        let schema = ExtensionResponseSchema::new(&application)
            .map_err(ModelRequestError::ResponseSchema)?;
        let mut params = json!({"schema":application,"instruction":instruction,"prompt":prompt});
        if let Some(model) = model {
            params["model"] = json!(model);
        }
        Ok(Self { params, schema })
    }

    #[must_use]
    pub fn params(&self) -> &Value {
        &self.params
    }

    pub fn decode_response(
        &self,
        response: &[u8],
    ) -> Result<StructuredModelOutput, ModelResponseError> {
        self.schema.decode(response)
    }
}

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
        let original = contract.document().map_err(ModelRequestError::Schema)?;
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

    #[must_use]
    pub fn provider(&self) -> StructuredProvider {
        self.provider
    }

    #[must_use]
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
