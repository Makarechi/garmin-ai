use std::{collections::BTreeMap, path::PathBuf};

use ga_app::extension_process::ExtensionProcess;
use ga_domain::event_payload::legacy_boolean;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::configuration;

type Executables = BTreeMap<String, PathBuf>;

struct RegisteredExtensions {
    source: Executables,
    channel: Executables,
    model: Executables,
}

impl RegisteredExtensions {
    fn capabilities(&self, instance: &Instance) -> &'static [&'static str] {
        let provider = instance.provider.as_bytes();
        if provider.len() > 100
            || !provider.first().is_some_and(u8::is_ascii_lowercase)
            || !provider.iter().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(byte)
            })
        {
            return &[];
        }
        let (registry, capabilities): (&_, &[_]) = match instance.kind.as_str() {
            "source" => (&self.source, &["observations"]),
            "channel" if instance.provider != "web" => (&self.channel, &["initiatives", "text"]),
            "model" => (&self.model, &["structured_output"]),
            _ => return &[],
        };
        if registry
            .get(&instance.id)
            .is_some_and(|path| ExtensionProcess::new(path, &instance.id).is_ok())
        {
            capabilities
        } else {
            &[]
        }
    }
}

fn executables(raw: Option<String>, bytes: usize, entries: usize) -> Executables {
    raw.filter(|raw| raw.len() <= bytes)
        .and_then(|raw| serde_json::from_str::<Executables>(&raw).ok())
        .filter(|registry| registry.len() <= entries)
        .unwrap_or_default()
}

#[derive(Deserialize)]
struct Instance {
    id: String,
    kind: String,
    provider: String,
    #[serde(default)]
    enabled: Option<Value>,
}

impl Instance {
    fn legacy(id: &str, kind: &str, provider: &str) -> Self {
        Self {
            id: id.to_owned(),
            kind: kind.to_owned(),
            provider: provider.to_owned(),
            enabled: None,
        }
    }

    fn declared(self, extensions: &RegisteredExtensions) -> Option<Value> {
        let enabled = self.enabled.as_ref().map_or(Some(true), legacy_boolean)?;
        let (capabilities, implementation): (&[&str], &str) =
            match (self.kind.as_str(), self.provider.as_str()) {
                ("source", "garmin") => (
                    &["activities", "fit_files", "health_metrics"],
                    "unversioned",
                ),
                ("channel", "telegram") => (&["actions", "initiatives", "text"], "unversioned"),
                ("model", "gemini") => (&["structured_output", "transcription"], "unversioned"),
                ("model", "openai") => (&["structured_output"], "openai-responses-v1"),
                ("model", "anthropic") => (&["structured_output"], "anthropic-messages-v1"),
                _ => (extensions.capabilities(&self), "unversioned"),
            };
        let reason = if !enabled {
            Some("integration is disabled")
        } else if capabilities.is_empty() {
            Some("integration provider is not registered")
        } else {
            None
        };
        let available = reason.is_none();
        Some(json!({
            "instance_id":self.id,"kind":self.kind,"provider":self.provider,
            "available":available,"capabilities":if available {capabilities} else {&[]},
            "reason":reason,"contract_version":available.then_some(1),
            "implementation_version":available.then_some(implementation),
            "verification_level":if available {"declared"} else {"unavailable"},
        }))
    }
}

pub(super) fn declared() -> Option<Vec<Value>> {
    let [
        explicit,
        token_dir,
        telegram_token,
        telegram_owner,
        llm,
        gemini_key,
        source_extensions,
        channel_extensions,
        model_extensions,
    ] = configuration::read([
        "GA_INTEGRATIONS",
        "GA_TOKEN_DIR",
        "GA_TELEGRAM_BOT_TOKEN",
        "GA_TELEGRAM_USER_ID",
        "GA_LLM_ENABLED",
        "GA_GEMINI_API_KEY",
        "GA_SOURCE_PLUGIN_EXECUTABLES",
        "GA_CHANNEL_PLUGIN_EXECUTABLES",
        "GA_NATIVE_MODEL_PLUGINS",
    ])
    .ok()?;
    let instances = if let Some(explicit) = explicit {
        if explicit.len() > super::MAX_REQUEST_BYTES {
            return None;
        }
        let declared: Vec<Instance> = serde_json::from_str(&explicit).ok()?;
        if declared.len() > 32 {
            return None;
        }
        declared
    } else {
        let mut legacy = Vec::new();
        let token_dir = PathBuf::from(token_dir.unwrap_or_else(|| "tokens/garmin".into()));
        if token_dir.join("garmin_tokens.json").is_file() {
            legacy.push(Instance::legacy(
                "source:garmin:primary",
                "source",
                "garmin",
            ));
        }
        if telegram_token.is_some_and(|token| !token.is_empty())
            && telegram_owner
                .and_then(|owner| owner.parse::<u64>().ok())
                .is_some_and(|owner| owner > 0)
        {
            legacy.push(Instance::legacy(
                "channel:telegram:primary",
                "channel",
                "telegram",
            ));
        }
        let llm = llm.is_some_and(|text| legacy_boolean(&Value::String(text)) == Some(true));
        if llm && gemini_key.is_some_and(|key| !key.is_empty()) {
            legacy.push(Instance::legacy("model:gemini:primary", "model", "gemini"));
        }
        legacy
    };
    let extensions = RegisteredExtensions {
        source: executables(source_extensions, super::MAX_REQUEST_BYTES, 32),
        channel: executables(channel_extensions, super::MAX_REQUEST_BYTES, 32),
        model: executables(model_extensions, 64 * 1024, usize::MAX),
    };
    instances
        .into_iter()
        .map(|instance| instance.declared(&extensions))
        .collect()
}
