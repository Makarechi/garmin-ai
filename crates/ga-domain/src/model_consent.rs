use std::{error::Error, fmt};

use crate::{model_response::StructuredProvider, timestamp::StoredInstant};

const CATEGORY_NAMES: [&str; 3] = ["health", "diary", "audio"];

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ModelCategories(u8);

impl ModelCategories {
    pub const HEALTH: Self = Self(1);
    pub const DIARY: Self = Self(2);
    pub const HEALTH_AND_DIARY: Self = Self(Self::HEALTH.0 | Self::DIARY.0);
    pub const AUDIO: Self = Self(4);

    pub fn from_labels<'a>(labels: impl IntoIterator<Item = &'a str>) -> Option<Self> {
        let mut categories = 0;
        for label in labels {
            let index = CATEGORY_NAMES.iter().position(|name| *name == label)?;
            categories |= 1 << index;
        }
        Some(Self(categories))
    }

    pub fn labels(self) -> impl Iterator<Item = &'static str> {
        CATEGORY_NAMES
            .into_iter()
            .enumerate()
            .filter_map(move |(bit, name)| (self.0 & (1 << bit) != 0).then_some(name))
    }

    fn covers(self, requested: Self) -> bool {
        self.0 & requested.0 == requested.0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelProvider {
    Gemini,
    Structured(StructuredProvider),
}

#[derive(Clone, Copy)]
pub enum ModelOperation {
    Configure,
    Structured(ModelCategories),
    Transcribe,
}

impl ModelOperation {
    pub fn categories(self) -> ModelCategories {
        match self {
            Self::Configure => ModelCategories::default(),
            Self::Structured(categories) => categories,
            Self::Transcribe => ModelCategories::AUDIO,
        }
    }
}

/// Identity of an already prepared client and its resolved credentials.
#[derive(Clone, Copy, Eq, PartialEq)]
pub struct ModelBinding<'a> {
    pub provider: ModelProvider,
    pub instance_id: &'a str,
    pub primary_model: &'a str,
    pub credential_generation: &'a str,
}

#[derive(Clone, Copy)]
pub struct ModelConfiguration<'a> {
    pub binding: ModelBinding<'a>,
    pub enabled: bool,
    pub structured_output: bool,
    pub fallback_enabled: bool,
    pub fallback_models: &'a [&'a str],
}

#[derive(Clone, Copy)]
pub struct ModelConsent<'a> {
    pub provider: ModelProvider,
    pub instance_id: &'a str,
    pub primary_model: &'a str,
    pub fallback_models: &'a [&'a str],
    pub categories: ModelCategories,
    pub granted_at: StoredInstant,
    pub policy_revision: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelConsentError {
    ConsentRequired,
    UnsupportedCapability,
    InvalidCategories,
}

impl fmt::Display for ModelConsentError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::ConsentRequired => "Model consent is missing or does not cover this request",
            Self::UnsupportedCapability => "The configured model does not support this operation",
            Self::InvalidCategories => "Structured model categories must be health and/or diary",
        })
    }
}
impl Error for ModelConsentError {}

/// Return the ordered candidates allowed by current settings, consent, and the prepared client.
/// A request may try only candidates returned for its operation.
pub fn authorized_models<'a>(
    prepared: ModelBinding<'_>,
    config: ModelConfiguration<'a>,
    consent: Option<ModelConsent<'_>>,
    operation: ModelOperation,
    now: StoredInstant,
) -> Result<Vec<&'a str>, ModelConsentError> {
    let binding = config.binding;
    match operation {
        ModelOperation::Structured(_) if !config.structured_output => {
            return Err(ModelConsentError::UnsupportedCapability);
        }
        ModelOperation::Transcribe if binding.provider != ModelProvider::Gemini => {
            return Err(ModelConsentError::UnsupportedCapability);
        }
        ModelOperation::Structured(categories)
            if categories.0 == 0 || categories.covers(ModelCategories::AUDIO) =>
        {
            return Err(ModelConsentError::InvalidCategories);
        }
        _ => {}
    }
    let consent = consent
        .filter(|consent| {
            config.enabled
                && prepared == binding
                && !binding.credential_generation.is_empty()
                && consent.provider == binding.provider
                && consent.instance_id == binding.instance_id
                && consent.primary_model == binding.primary_model
                && consent.policy_revision == 1
                && consent.granted_at <= now
                && consent.categories.covers(operation.categories())
        })
        .ok_or(ModelConsentError::ConsentRequired)?;
    let mut models = vec![binding.primary_model];
    if binding.provider == ModelProvider::Gemini && config.fallback_enabled {
        for &candidate in config.fallback_models {
            if consent.fallback_models.contains(&candidate) && !models.contains(&candidate) {
                models.push(candidate);
            }
        }
    }
    Ok(models)
}
