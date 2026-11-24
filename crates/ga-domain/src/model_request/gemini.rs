mod fingerprint;

use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};

use super::{ApprovedModelSchema, ModelRequestError};
use crate::{
    model_response::{GeminiResponseSchema, ModelResponseError, StructuredModelOutput},
    model_schema::{ModelSchemaError, compile_gemini_schema},
};

const VOICE_INSTRUCTION: &str = include_str!("../../contracts/model-prompts/voice-transcribe.txt");
pub const MAX_GEMINI_AUDIO_BYTES: usize = 20 * 1024 * 1024;

pub struct GeminiModelRequest {
    body: Value,
    schema: GeminiResponseSchema,
}

impl GeminiModelRequest {
    pub fn structured(
        contract: ApprovedModelSchema,
        model: &str,
        thinking_level: &str,
        instruction: &str,
        prompt: &str,
    ) -> Result<Self, ModelRequestError> {
        check_model(model)?;
        let original = contract.document().map_err(ModelRequestError::Schema)?;
        let application = serde_json::from_slice(original)
            .map_err(|_| ModelRequestError::Schema(ModelSchemaError::InvalidDocument))?;
        let wire = compile_gemini_schema(original).map_err(ModelRequestError::Schema)?;
        Self::prepare(
            model,
            thinking_level,
            instruction,
            json!(prompt),
            wire,
            &application,
        )
    }

    /// Build the Gemini-only voice request after the application's audio consent check.
    pub fn transcribe(
        model: &str,
        thinking_level: &str,
        audio: &[u8],
        mime_type: &str,
    ) -> Result<Self, ModelRequestError> {
        check_model(model)?;
        if audio.len() > MAX_GEMINI_AUDIO_BYTES {
            return Err(ModelRequestError::InputTooLarge);
        }
        let schema = json!({
            "properties": {"text": {"title":"Text","type":"string"}},
            "required": ["text"],
            "title":"Transcript",
            "type":"object",
        });
        let mut input = json!([{
            "content": [{"data": null, "mime_type": mime_type, "type":"audio"}],
            "type": "user_input",
        }]);
        input[0]["content"][0]["data"] = Value::String(STANDARD.encode(audio));
        Self::prepare(
            model,
            thinking_level,
            VOICE_INSTRUCTION.trim(),
            input,
            schema.clone(),
            &schema,
        )
    }

    fn prepare(
        model: &str,
        thinking_level: &str,
        instruction: &str,
        input: Value,
        wire: Value,
        application: &Value,
    ) -> Result<Self, ModelRequestError> {
        let schema =
            GeminiResponseSchema::new(application).map_err(ModelRequestError::ResponseSchema)?;
        let generation = if thinking_level.is_empty() {
            json!({})
        } else {
            json!({"thinking_level":thinking_level})
        };
        let mut body = json!({
            "input": null,
            "model": model,
            "generation_config": generation,
            "response_format": {"mime_type":"application/json", "schema":wire, "type":"text"},
            "store": false,
            "system_instruction": instruction,
        });
        body["input"] = input;
        Ok(Self { body, schema })
    }

    /// Switch the candidate without rebuilding the schema or copying inline audio.
    pub fn select_model(&mut self, model: &str) -> Result<(), ModelRequestError> {
        check_model(model)?;
        self.body["model"] = json!(model);
        Ok(())
    }

    #[must_use]
    pub fn body(&self) -> &Value {
        &self.body
    }

    pub fn decode_response(
        &self,
        response: &[u8],
    ) -> Result<StructuredModelOutput, ModelResponseError> {
        self.schema.decode(response)
    }
}

fn check_model(model: &str) -> Result<(), ModelRequestError> {
    if (1..=200).contains(&model.chars().count()) {
        Ok(())
    } else {
        Err(ModelRequestError::InvalidConfiguration)
    }
}
