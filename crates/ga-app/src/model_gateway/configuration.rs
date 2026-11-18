use std::{collections::BTreeMap, error::Error, fmt, time::Duration};

use ga_db::{
    Database,
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::{
    event_payload::legacy_boolean,
    model_consent::{
        ModelBinding, ModelCategories, ModelConfiguration, ModelConsent, ModelOperation,
        ModelProvider, authorized_models,
    },
    model_gate::{ModelGateIdentity, PRIMARY_GEMINI_INSTANCE},
    model_response::StructuredProvider,
    sha256_hex,
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::Value;

use super::{
    CurrentModelPolicy, GatewayError, GeminiOptions, ModelGateway, SelectedModel, StructuredOptions,
};

#[cfg(test)]
mod tests;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelEnvironmentError {
    Invalid,
    Multiple,
    UnsupportedProvider,
}

impl fmt::Display for ModelEnvironmentError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        output.write_str(match self {
            Self::Invalid => "Model integration configuration is invalid",
            Self::Multiple => "Only one enabled model instance can be configured",
            Self::UnsupportedProvider => "The selected model provider has no native runtime",
        })
    }
}
impl Error for ModelEnvironmentError {}

pub struct RuntimeModelSettings {
    instance: String,
    model: String,
    provider: ModelProvider,
    generation: String,
    deadline: Duration,
    options: ProviderSettings,
    consent: Option<SavedConsent>,
}

enum ProviderSettings {
    Gemini {
        key: String,
        thinking: String,
        fallback_enabled: bool,
        fallback: Vec<String>,
    },
    Structured {
        key: String,
        max_tokens: u16,
        timeout: Duration,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SavedConsent {
    provider: String,
    #[serde(default = "primary_gemini")]
    provider_instance_id: String,
    model: String,
    #[serde(default)]
    fallback_models: Vec<String>,
    categories: Vec<String>,
    granted_at: String,
    policy_revision: u8,
}

fn primary_gemini() -> String {
    PRIMARY_GEMINI_INSTANCE.into()
}

/// Owns the borrowed model and consent lists for the live gateway.
pub struct ConfiguredModel<'a> {
    settings: &'a RuntimeModelSettings,
    fallback: Vec<&'a str>,
    approved: Vec<&'a str>,
}

impl RuntimeModelSettings {
    /// Resolve storage-neutral settings while reading only the selected provider's credential.
    pub fn resolve(
        mut variable: impl FnMut(&str) -> Result<Option<String>, ModelEnvironmentError>,
        mut secret: impl FnMut(&str) -> Result<Option<String>, ModelEnvironmentError>,
    ) -> Result<Option<Self>, ModelEnvironmentError> {
        use ModelEnvironmentError::Invalid;

        let integrations = variable("GA_INTEGRATIONS")?
            .map(|raw| serde_json::from_str::<Vec<Value>>(&raw).map_err(|_| Invalid))
            .transpose()?
            .unwrap_or_default();
        let mut enabled_models = Vec::new();
        if integrations.len() > 32 {
            return Err(Invalid);
        }
        for spec in &integrations {
            if spec["kind"] == "model"
                && spec
                    .get("enabled")
                    .map_or(Some(true), legacy_boolean)
                    .ok_or(Invalid)?
            {
                enabled_models.push(spec);
            }
        }
        let configured = match enabled_models.as_slice() {
            [] if integrations.is_empty() => None,
            [] => return Ok(None),
            [spec] => Some(*spec),
            _ => return Err(ModelEnvironmentError::Multiple),
        };
        if !setting_bool(variable("GA_LLM_ENABLED")?, false)? {
            return Ok(None);
        }
        let (provider, instance) = if let Some(spec) = configured {
            let instance = spec["id"].as_str().ok_or(Invalid)?;
            let provider = parse_provider(spec["provider"].as_str().ok_or(Invalid)?)?;
            (provider, instance)
        } else {
            (ModelProvider::Gemini, PRIMARY_GEMINI_INSTANCE)
        };
        ShareDestination::new(DestinationKind::Model, instance).map_err(|_| Invalid)?;
        let timeout = variable("GA_MODEL_REQUEST_TIMEOUT_SECONDS")?
            .map(|raw| raw.parse::<f64>().map_err(|_| Invalid))
            .transpose()?
            .unwrap_or(60.0);
        let timeout = bounded_timeout(timeout, 0.05, 300.0)?;
        let deadline = configured
            .and_then(|spec| spec.get("request_timeout_seconds"))
            .filter(|item| !item.is_null())
            .map(|item| bounded_timeout(number(item).ok_or(Invalid)?, 0.05, 300.0))
            .transpose()?
            .unwrap_or_else(|| {
                if provider == ModelProvider::Gemini {
                    Duration::from_secs(110)
                } else {
                    timeout
                }
            });
        let consent = variable("GA_LLM_CONSENT")?
            .map(|raw| serde_json::from_str::<Option<SavedConsent>>(&raw).map_err(|_| Invalid))
            .transpose()?
            .flatten();
        if let Some(consent) = &consent {
            consent.validate()?;
        }
        let (model, options) = match provider {
            ModelProvider::Gemini => {
                let (Some(model), Some(key)) = (
                    present(variable("GA_GEMINI_MODEL")?),
                    present(variable("GA_GEMINI_API_KEY")?),
                ) else {
                    return Ok(None);
                };
                let thinking = variable("GA_GEMINI_THINKING_LEVEL")?.unwrap_or_default();
                let fallback_enabled = setting_bool(variable("GA_GEMINI_FALLBACK_ENABLED")?, true)?;
                let fallback = variable("GA_GEMINI_FALLBACK_MODELS")?
                    .map(|raw| serde_json::from_str(&raw).map_err(|_| Invalid))
                    .transpose()?
                    .unwrap_or_else(|| vec!["gemini-3.1-flash-lite".into()]);
                validate_models(&fallback)?;
                (
                    model,
                    ProviderSettings::Gemini {
                        key,
                        thinking,
                        fallback_enabled,
                        fallback,
                    },
                )
            }
            ModelProvider::Structured(_) => {
                let spec = configured.ok_or(Invalid)?;
                let config = spec["config"].as_object().ok_or(Invalid)?;
                if config.keys().any(|name| {
                    !matches!(
                        name.as_str(),
                        "model" | "structured_output" | "timeout_seconds" | "max_output_tokens"
                    )
                }) || config.get("structured_output").and_then(Value::as_bool) != Some(true)
                {
                    return Err(Invalid);
                }
                let model = config
                    .get("model")
                    .and_then(Value::as_str)
                    .ok_or(Invalid)?
                    .to_owned();
                let timeout = config
                    .get("timeout_seconds")
                    .map(|v| bounded_timeout(number(v).ok_or(Invalid)?, 1.0, 120.0))
                    .transpose()?
                    .unwrap_or(Duration::from_secs(60));
                let max_tokens = config
                    .get("max_output_tokens")
                    .map(|v| {
                        number(v)
                            .filter(|n| (256.0..=8192.0).contains(n) && n.fract() == 0.0)
                            .map(|n| n as u16)
                            .ok_or(Invalid)
                    })
                    .transpose()?
                    .unwrap_or(4096);
                let refs = spec["secret_refs"]
                    .as_object()
                    .filter(|refs| refs.len() == 1)
                    .ok_or(Invalid)?;
                let reference = refs
                    .get("api_key")
                    .and_then(Value::as_str)
                    .filter(|name| valid_secret_name(name))
                    .ok_or(Invalid)?;
                let Some(key) = present(secret(reference)?) else {
                    return Ok(None);
                };
                (
                    model,
                    ProviderSettings::Structured {
                        key,
                        max_tokens,
                        timeout,
                    },
                )
            }
        };
        if !valid_model(&model) {
            return Err(Invalid);
        }
        let generation = match &options {
            ProviderSettings::Gemini { key, .. } => sha256_hex(key.as_bytes()),
            ProviderSettings::Structured { key, .. } => {
                let provider = configured
                    .and_then(|spec| spec["provider"].as_str())
                    .ok_or(Invalid)?;
                ModelGateIdentity::plugin(
                    instance,
                    provider,
                    &BTreeMap::from([("api_key", Some(key.as_str()))]),
                )
                .map_err(|_| Invalid)?
                .configuration()
                .to_owned()
            }
        };
        Ok(Some(Self {
            instance: instance.into(),
            model,
            provider,
            generation,
            deadline,
            options,
            consent,
        }))
    }

    pub fn configured(&self) -> ConfiguredModel<'_> {
        let fallback = match &self.options {
            ProviderSettings::Gemini { fallback, .. } => {
                fallback.iter().map(String::as_str).collect()
            }
            ProviderSettings::Structured { .. } => Vec::new(),
        };
        let approved = self
            .consent
            .iter()
            .flat_map(|grant| grant.fallback_models.iter().map(String::as_str))
            .collect();
        ConfiguredModel {
            settings: self,
            fallback,
            approved,
        }
    }
}

impl ConfiguredModel<'_> {
    pub fn gateway(
        &self,
        database: Database,
        notify_quota: bool,
        now: StoredInstant,
    ) -> Result<ModelGateway<'_>, GatewayError> {
        let current = self.current();
        authorized_models(
            current.configuration.binding,
            current.configuration,
            current.consent,
            ModelOperation::Configure,
            now,
        )
        .map_err(GatewayError::Consent)?;
        let selected = SelectedModel {
            instance_id: &self.settings.instance,
            primary_model: &self.settings.model,
            deadline: self.settings.deadline,
        };
        match &self.settings.options {
            ProviderSettings::Gemini {
                key,
                thinking,
                fallback_enabled,
                ..
            } => ModelGateway::gemini(
                database,
                selected,
                GeminiOptions {
                    key,
                    thinking_level: thinking,
                    fallback_enabled: *fallback_enabled,
                    fallback_models: &self.fallback,
                    consent_models: &self.approved,
                    notify_quota,
                },
            ),
            ProviderSettings::Structured {
                key,
                max_tokens,
                timeout,
            } => {
                let ModelProvider::Structured(provider) = self.settings.provider else {
                    return Err(GatewayError::Configuration);
                };
                ModelGateway::structured(
                    database,
                    selected,
                    StructuredOptions {
                        provider,
                        secrets: &BTreeMap::from([("api_key", Some(key.as_str()))]),
                        max_output_tokens: *max_tokens,
                        transport_timeout: *timeout,
                    },
                )
            }
        }
    }

    pub fn current(&self) -> CurrentModelPolicy<'_> {
        let fallback_enabled = matches!(
            self.settings.options,
            ProviderSettings::Gemini {
                fallback_enabled: true,
                ..
            }
        );
        CurrentModelPolicy {
            configuration: ModelConfiguration {
                binding: ModelBinding {
                    provider: self.settings.provider,
                    instance_id: &self.settings.instance,
                    primary_model: &self.settings.model,
                    credential_generation: &self.settings.generation,
                },
                enabled: true,
                structured_output: true,
                fallback_enabled,
                fallback_models: &self.fallback,
            },
            consent: self.settings.consent.as_ref().and_then(|grant| {
                Some(ModelConsent {
                    provider: parse_provider(&grant.provider).ok()?,
                    instance_id: &grant.provider_instance_id,
                    primary_model: &grant.model,
                    fallback_models: &self.approved,
                    categories: ModelCategories::from_labels(
                        grant.categories.iter().map(String::as_str),
                    )?,
                    granted_at: grant.granted_at.parse().ok()?,
                    policy_revision: grant.policy_revision,
                })
            }),
        }
    }
}

impl SavedConsent {
    fn validate(&self) -> Result<(), ModelEnvironmentError> {
        let invalid = || ModelEnvironmentError::Invalid;
        ShareDestination::new(DestinationKind::Model, &self.provider_instance_id)
            .map_err(|_| invalid())?;
        self.granted_at
            .parse::<StoredInstant>()
            .map_err(|_| invalid())?;
        if !valid_model(&self.model)
            || self.policy_revision != 1
            || self.categories.is_empty()
            || ModelCategories::from_labels(self.categories.iter().map(String::as_str)).is_none()
        {
            return Err(invalid());
        }
        validate_models(&self.fallback_models)
    }
}

fn parse_provider(value: &str) -> Result<ModelProvider, ModelEnvironmentError> {
    match value {
        "gemini" => Ok(ModelProvider::Gemini),
        "openai" => Ok(ModelProvider::Structured(StructuredProvider::OpenAi)),
        "anthropic" => Ok(ModelProvider::Structured(StructuredProvider::Anthropic)),
        _ => Err(ModelEnvironmentError::UnsupportedProvider),
    }
}

fn present(value: Option<String>) -> Option<String> {
    value.filter(|value| !value.is_empty())
}

fn setting_bool(value: Option<String>, fallback: bool) -> Result<bool, ModelEnvironmentError> {
    value
        .map_or(Some(fallback), |value| {
            legacy_boolean(&Value::String(value))
        })
        .ok_or(ModelEnvironmentError::Invalid)
}

fn valid_model(value: &str) -> bool {
    (1..=200).contains(&value.chars().count())
}

fn validate_models(models: &[String]) -> Result<(), ModelEnvironmentError> {
    if models.len() <= 8 && models.iter().all(|model| valid_model(model)) {
        Ok(())
    } else {
        Err(ModelEnvironmentError::Invalid)
    }
}

fn number(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str()?.trim().parse().ok())
}

fn bounded_timeout(seconds: f64, min: f64, max: f64) -> Result<Duration, ModelEnvironmentError> {
    if (min..=max).contains(&seconds) {
        Ok(Duration::from_secs_f64(seconds))
    } else {
        Err(ModelEnvironmentError::Invalid)
    }
}

fn valid_secret_name(name: &str) -> bool {
    let Some(rest) = name.strip_prefix("GA_PLUGIN_") else {
        return false;
    };
    let mut bytes = rest.bytes();
    rest.len() <= 128
        && bytes.next().is_some_and(|byte| byte.is_ascii_uppercase())
        && bytes.all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
}
