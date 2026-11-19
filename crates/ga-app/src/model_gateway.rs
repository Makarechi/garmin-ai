mod analysis;
mod configuration;
mod interpretation;
mod safety;
mod tracker;

pub use analysis::{
    AnalysisDestination, AnalysisError, AnalysisQuestion, AnalysisReadTools, AnalysisResult,
    AnalysisToolRead, DiaryAnalysisTools, OfferedReadTool,
};
pub use configuration::{ConfiguredModel, ModelEnvironmentError, RuntimeModelSettings};
pub use interpretation::{
    DiaryInterpretationError, DiaryInterpretationRequest, ModelDiaryContext,
    TelegramDiaryInterpretation,
};
pub use safety::{ReplySafety, ReplySafetyIntent, SafetyScreenError};

mod runtime;

#[cfg(test)]
mod tests;

use std::{collections::BTreeMap, error::Error, fmt, future::Future, time::Duration};

use ga_db::{
    Database,
    model_gate::ModelGateError,
    share_policy::{DestinationKind, ModelChannelSharing, ShareDestination, ShareRequirement},
};
use ga_domain::{
    model_consent::{
        ModelBinding, ModelCategories, ModelConfiguration, ModelConsent, ModelConsentError,
        ModelOperation, ModelProvider, authorized_models,
    },
    model_gate::ModelGateIdentity,
    model_request::{
        ApprovedModelSchema, GeminiModelRequest, ModelRequestError, StructuredModelRequest,
    },
    model_response::{StructuredModelOutput, StructuredProvider},
    sha256_hex,
    timestamp::StoredInstant,
};
use ga_model::{GeminiTransport, ModelTransport, ModelTransportError, fallback::FallbackError};
use uuid::Uuid;

const MAX_GEMINI_ATTEMPT: Duration = Duration::from_secs(60);

pub struct SelectedModel<'a> {
    pub instance_id: &'a str,
    pub primary_model: &'a str,
    pub deadline: Duration,
}

pub struct GeminiOptions<'a> {
    pub key: &'a str,
    pub fallback_enabled: bool,
    pub fallback_models: &'a [&'a str],
    pub consent_models: &'a [&'a str],
    pub thinking_level: &'a str,
    pub notify_quota: bool,
}

pub struct StructuredOptions<'a> {
    pub provider: StructuredProvider,
    pub secrets: &'a BTreeMap<&'a str, Option<&'a str>>,
    pub max_output_tokens: u16,
    pub transport_timeout: Duration,
}

pub enum ModelInput<'a> {
    Structured {
        contract: ApprovedModelSchema,
        instruction: &'a str,
        prompt: &'a str,
        categories: ModelCategories,
    },
    Voice {
        audio: &'a [u8],
        mime_type: &'a str,
    },
}

impl ModelInput<'_> {
    fn operation(&self) -> ModelOperation {
        match self {
            Self::Structured { categories, .. } => ModelOperation::Structured(*categories),
            Self::Voice { .. } => ModelOperation::Transcribe,
        }
    }
}

#[derive(Clone, Copy)]
pub struct ActiveModelJob {
    pub id: Uuid,
    pub lease_token: Uuid,
}

#[derive(Clone, Copy)]
pub struct CurrentModelPolicy<'a> {
    pub configuration: ModelConfiguration<'a>,
    pub consent: Option<ModelConsent<'a>>,
}

#[derive(Debug)]
pub enum GatewayError {
    Configuration,
    Clock,
    Consent(ModelConsentError),
    Request(ModelRequestError),
    State(ModelGateError),
    Attempt(FallbackError),
}

impl fmt::Display for GatewayError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration => f.write_str("Selected model configuration is invalid"),
            Self::Clock => f.write_str("Model request clock is unavailable"),
            Self::Consent(error) => error.fmt(f),
            Self::Request(error) => error.fmt(f),
            Self::State(error) => error.fmt(f),
            Self::Attempt(FallbackError::ConsentRequired) => {
                f.write_str("Model consent was withdrawn")
            }
            Self::Attempt(FallbackError::StateUnavailable) => {
                f.write_str("Model request permissions are unavailable")
            }
            Self::Attempt(FallbackError::Cooldown { .. }) => {
                f.write_str("Model is temporarily unavailable")
            }
            Self::Attempt(FallbackError::Deadline) => f.write_str("Model request deadline expired"),
            Self::Attempt(FallbackError::NoEligibleModel) => {
                f.write_str("There is no eligible model")
            }
            Self::Attempt(FallbackError::Provider(error)) => error.fmt(f),
        }
    }
}
impl Error for GatewayError {}

pub struct ModelGateway<'a> {
    database: Database,
    selected: SelectedModel<'a>,
    identity: ModelGateIdentity,
    provider: ModelProvider,
    generation: String,
    request_options: RequestOptions<'a>,
    transport: Transport,
    notify_quota: bool,
}

#[derive(Clone, Copy)]
enum RequestOptions<'a> {
    Gemini {
        thinking_level: &'a str,
        fallback_enabled: bool,
        fallback_models: &'a [&'a str],
        consent_models: &'a [&'a str],
    },
    Structured {
        provider: StructuredProvider,
        max_output_tokens: u16,
    },
}

enum Transport {
    Gemini(GeminiTransport),
    Structured(ModelTransport),
}

pub(crate) enum PreparedRequest {
    Gemini(GeminiModelRequest),
    Structured(StructuredModelRequest),
}

pub(crate) trait Sender: Sync {
    fn send(
        &self,
        request: &PreparedRequest,
        remaining: Duration,
    ) -> impl Future<Output = Result<StructuredModelOutput, ModelTransportError>> + Send;
}

impl Sender for Transport {
    async fn send(
        &self,
        request: &PreparedRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        match (self, request) {
            (Self::Gemini(transport), PreparedRequest::Gemini(request)) => {
                transport
                    .send(
                        request,
                        remaining
                            .min(MAX_GEMINI_ATTEMPT)
                            .max(Duration::from_millis(1)),
                    )
                    .await
            }
            (Self::Structured(transport), PreparedRequest::Structured(request)) => {
                transport.send(request).await
            }
            _ => Err(ModelTransportError::InvalidConfiguration),
        }
    }
}

impl<'a> ModelGateway<'a> {
    pub(crate) fn sender(&self) -> &impl Sender {
        &self.transport
    }
    pub fn gemini(
        database: Database,
        selected: SelectedModel<'a>,
        options: GeminiOptions<'a>,
    ) -> Result<Self, GatewayError> {
        validate_selection(&selected)?;
        if options.fallback_models.len() > 8 || options.consent_models.len() > 8 {
            return Err(GatewayError::Configuration);
        }
        let identity = ModelGateIdentity::gemini(
            selected.instance_id,
            selected.primary_model,
            options.fallback_enabled,
            options.fallback_models,
            options.consent_models,
            options.key,
        )
        .map_err(|_| GatewayError::Configuration)?;
        let transport =
            GeminiTransport::new(options.key).map_err(|_| GatewayError::Configuration)?;
        Ok(Self {
            database,
            selected,
            identity,
            provider: ModelProvider::Gemini,
            generation: sha256_hex(options.key.as_bytes()),
            request_options: RequestOptions::Gemini {
                thinking_level: options.thinking_level,
                fallback_enabled: options.fallback_enabled,
                fallback_models: options.fallback_models,
                consent_models: options.consent_models,
            },
            transport: Transport::Gemini(transport),
            notify_quota: options.notify_quota,
        })
    }

    pub fn structured(
        database: Database,
        selected: SelectedModel<'a>,
        options: StructuredOptions<'_>,
    ) -> Result<Self, GatewayError> {
        validate_selection(&selected)?;
        let key = options
            .secrets
            .get("api_key")
            .and_then(|key| *key)
            .ok_or(GatewayError::Configuration)?;
        let provider = match options.provider {
            StructuredProvider::OpenAi => "openai",
            StructuredProvider::Anthropic => "anthropic",
        };
        let identity = ModelGateIdentity::plugin(selected.instance_id, provider, options.secrets)
            .map_err(|_| GatewayError::Configuration)?;
        let transport = ModelTransport::new(options.provider, key, options.transport_timeout)
            .map_err(|_| GatewayError::Configuration)?;
        Ok(Self {
            database,
            selected,
            generation: identity.configuration().to_owned(),
            identity,
            provider: ModelProvider::Structured(options.provider),
            request_options: RequestOptions::Structured {
                provider: options.provider,
                max_output_tokens: options.max_output_tokens,
            },
            transport: Transport::Structured(transport),
            notify_quota: false,
        })
    }

    pub fn binding(&self) -> ModelBinding<'_> {
        ModelBinding {
            provider: self.provider,
            instance_id: self.selected.instance_id,
            primary_model: self.selected.primary_model,
            credential_generation: &self.generation,
        }
    }

    /// Evaluate live policy before sending anything and again before every candidate.
    /// Callers identify the exact tracker versions that contributed to the prepared input.
    pub async fn execute<'policy>(
        &self,
        input: ModelInput<'_>,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
        requirements: &[ShareRequirement<'_>],
        job: Option<ActiveModelJob>,
    ) -> Result<StructuredModelOutput, GatewayError> {
        self.execute_with(input, &policy, requirements, job, &self.transport)
            .await
    }

    /// Keep both consent scopes current when a channel supplied diary context.
    pub async fn execute_in_channel<'policy>(
        &self,
        input: ModelInput<'_>,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
        requirements: &[ShareRequirement<'_>],
        channel: ModelChannelSharing<'_>,
        job: Option<ActiveModelJob>,
    ) -> Result<StructuredModelOutput, GatewayError> {
        self.execute_with_context(
            input,
            &policy,
            runtime::ModelRequestScope {
                requirements,
                channel: Some(channel),
                job,
            },
            &self.transport,
        )
        .await
    }

    fn authorize<'policy>(
        &self,
        current: CurrentModelPolicy<'policy>,
        operation: ModelOperation,
        now: StoredInstant,
    ) -> Result<Vec<&'policy str>, ModelConsentError> {
        let authorized = authorized_models(
            self.binding(),
            current.configuration,
            current.consent,
            operation,
            now,
        )?;
        if let RequestOptions::Gemini {
            fallback_enabled,
            fallback_models,
            consent_models,
            ..
        } = self.request_options
            && (current.configuration.fallback_enabled != fallback_enabled
                || current.configuration.fallback_models != fallback_models
                || current
                    .consent
                    .is_none_or(|grant| grant.fallback_models != consent_models))
        {
            return Err(ModelConsentError::ConsentRequired);
        }
        Ok(authorized)
    }

    fn prepare(&self, input: ModelInput<'_>) -> Result<PreparedRequest, GatewayError> {
        let model = self.selected.primary_model;
        match (self.request_options, input) {
            (
                RequestOptions::Gemini { thinking_level, .. },
                ModelInput::Structured {
                    contract,
                    instruction,
                    prompt,
                    ..
                },
            ) => {
                GeminiModelRequest::structured(contract, model, thinking_level, instruction, prompt)
                    .map(PreparedRequest::Gemini)
            }
            (
                RequestOptions::Gemini { thinking_level, .. },
                ModelInput::Voice { audio, mime_type },
            ) => GeminiModelRequest::transcribe(model, thinking_level, audio, mime_type)
                .map(PreparedRequest::Gemini),
            (
                RequestOptions::Structured {
                    provider,
                    max_output_tokens,
                },
                ModelInput::Structured {
                    contract,
                    instruction,
                    prompt,
                    ..
                },
            ) => StructuredModelRequest::prepare(
                provider,
                contract,
                model,
                max_output_tokens,
                instruction,
                prompt,
            )
            .map(PreparedRequest::Structured),
            (RequestOptions::Structured { .. }, ModelInput::Voice { .. }) => {
                return Err(GatewayError::Consent(
                    ModelConsentError::UnsupportedCapability,
                ));
            }
        }
        .map_err(GatewayError::Request)
    }
}

fn validate_selection(selection: &SelectedModel<'_>) -> Result<(), GatewayError> {
    ShareDestination::new(DestinationKind::Model, selection.instance_id)
        .map_err(|_| GatewayError::Configuration)?;
    if !(1..=200).contains(&selection.primary_model.chars().count())
        || !(Duration::from_millis(50)..=Duration::from_secs(300)).contains(&selection.deadline)
    {
        return Err(GatewayError::Configuration);
    }
    Ok(())
}
