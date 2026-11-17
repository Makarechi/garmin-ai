use std::collections::BTreeMap;

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{
    legacy_json::{JsonError, Utf8Layout, encode_ascii},
    sha256_hex,
    timestamp::StoredInstant,
};

pub const PRIMARY_GEMINI_INSTANCE: &str = "model:gemini:primary";
pub const PRIMARY_GEMINI_STATE: &str = "provider:gemini:gate";
const PRIMARY_GEMINI_LOCK: i64 = 72104634;
const MAX_CONFIGURATION_BYTES: usize = 64 * 1024;

/// The existing state, resume and PostgreSQL lock identities for an integration.
pub struct ModelGateIdentity {
    state_key: String,
    lock_key: i64,
    configuration: String,
    resume_configuration: String,
    inherits_legacy_gemini_state: bool,
}

impl ModelGateIdentity {
    pub fn gemini(
        instance: &str,
        model: &str,
        fallback_enabled: bool,
        fallback_models: &[&str],
        consent_models: &[&str],
        api_key: &str,
    ) -> Result<Self, JsonError> {
        let configuration = configuration_hash(&json!([
            model,
            fallback_enabled,
            fallback_models,
            consent_models,
            api_key
        ]))?;
        if instance == PRIMARY_GEMINI_INSTANCE {
            return Ok(Self {
                state_key: PRIMARY_GEMINI_STATE.to_owned(),
                lock_key: PRIMARY_GEMINI_LOCK,
                resume_configuration: configuration.clone(),
                configuration,
                inherits_legacy_gemini_state: false,
            });
        }
        let scope = format!("{instance}\0{configuration}");
        let state_key = format!("{PRIMARY_GEMINI_STATE}:{}", sha256_hex(scope.as_bytes()));
        Ok(Self::scoped(
            state_key,
            scope.as_bytes(),
            configuration,
            true,
        ))
    }

    /// Secret values are resolved by the integration configuration. Missing values hash as null.
    pub fn plugin(
        instance: &str,
        provider: &str,
        secrets: &BTreeMap<&str, Option<&str>>,
    ) -> Result<Self, JsonError> {
        let identity = configuration_bytes(&json!([instance, provider, secrets]))?;
        let configuration = sha256_hex(identity.as_bytes());
        let state_key = format!("provider:model:gate:{configuration}");
        Ok(Self::scoped(
            state_key,
            identity.as_bytes(),
            configuration,
            false,
        ))
    }

    fn scoped(
        state_key: String,
        scope: &[u8],
        configuration: String,
        inherits_legacy_gemini_state: bool,
    ) -> Self {
        let digest: [u8; 32] = Sha256::digest(scope).into();
        let [a, b, c, d, e, f, g, h, ..] = digest;
        let lock_key = i64::from_be_bytes([a, b, c, d, e, f, g, h]);
        let resume_configuration = sha256_hex(format!("{state_key}\0{configuration}").as_bytes());
        Self {
            state_key,
            lock_key,
            configuration,
            resume_configuration,
            inherits_legacy_gemini_state,
        }
    }

    pub fn state_key(&self) -> &str {
        &self.state_key
    }

    pub fn lock_key(&self) -> i64 {
        self.lock_key
    }

    pub fn configuration(&self) -> &str {
        &self.configuration
    }

    pub fn resume_configuration(&self) -> &str {
        &self.resume_configuration
    }

    pub fn inherits_legacy_gemini_state(&self) -> bool {
        self.inherits_legacy_gemini_state
    }
}

fn configuration_bytes(value: &Value) -> Result<String, JsonError> {
    let json = serde_json::to_vec(value).map_err(|_| JsonError::InvalidJson)?;
    encode_ascii(&json, Utf8Layout::Compact, MAX_CONFIGURATION_BYTES)
}

fn configuration_hash(value: &Value) -> Result<String, JsonError> {
    Ok(sha256_hex(configuration_bytes(value)?.as_bytes()))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelGateReason {
    Busy,
    Quota,
    Auth,
    Model,
    ModelCooldown,
    Unavailable,
}

pub struct ModelGatePause {
    pub until: StoredInstant,
    pub reason: ModelGateReason,
}

#[derive(Default)]
pub struct ModelGateState {
    pub blocked: Option<ModelGatePause>,
    pub scheduler_pause_until: Option<StoredInstant>,
    pub model_cooldowns: BTreeMap<String, StoredInstant>,
}

impl ModelGateState {
    pub fn read(value: &Value, identity: &ModelGateIdentity, now: StoredInstant) -> Self {
        if value["configuration"].as_str() != Some(identity.configuration()) {
            return Self::default();
        }
        let active = |deadline: &Value| deadline.as_str()?.parse().ok().filter(|at| *at > now);
        let blocked = active(&value["blocked_until"]).map(|until| ModelGatePause {
            until,
            reason: match value["reason"].as_str() {
                Some("busy") => ModelGateReason::Busy,
                Some("quota") => ModelGateReason::Quota,
                Some("auth") => ModelGateReason::Auth,
                Some("model") => ModelGateReason::Model,
                Some("model_cooldown") => ModelGateReason::ModelCooldown,
                _ => ModelGateReason::Unavailable,
            },
        });
        let model_cooldowns = value["model_cooldowns"]
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(model, deadline)| Some((model.clone(), active(deadline)?)))
            .collect();
        Self {
            blocked,
            scheduler_pause_until: active(&value["scheduler_pause_until"]),
            model_cooldowns,
        }
    }

    pub fn scheduler_paused(&self) -> bool {
        self.blocked.is_some() || self.scheduler_pause_until.is_some()
    }
}
