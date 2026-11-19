use std::collections::{BTreeMap, BTreeSet};

use ga_app::telegram_settings::TelegramIngressSettings;
use ga_domain::event_payload::legacy_boolean;
use serde::Deserialize;
use serde_json::Value;

use super::INVALID_SETTINGS;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Integration {
    id: String,
    kind: String,
    provider: String,
    #[serde(default = "enabled_default")]
    enabled: Value,
    request_timeout_seconds: Option<Value>,
    #[serde(default)]
    config: BTreeMap<String, Value>,
    #[serde(default)]
    secret_refs: BTreeMap<String, String>,
}

pub(super) fn validate(
    integrations: Option<&str>,
    owner: Option<&str>,
) -> Result<(), &'static str> {
    TelegramIngressSettings::selected(owner, integrations).map_err(|_| INVALID_SETTINGS)?;
    let Some(integrations) = integrations else {
        return Ok(());
    };
    let entries: Vec<Integration> =
        serde_json::from_str(integrations).map_err(|_| INVALID_SETTINGS)?;
    let mut ids = BTreeSet::new();
    if entries
        .iter()
        .any(|entry| !ids.insert(&entry.id) || !entry.valid())
    {
        return Err(INVALID_SETTINGS);
    }
    Ok(())
}

impl Integration {
    fn valid(&self) -> bool {
        matches!(self.kind.as_str(), "channel" | "source" | "model")
            && identifier(&self.id, 200, u8::is_ascii_lowercase, b"_.:-")
            && identifier(&self.provider, 100, u8::is_ascii_lowercase, b"_.-")
            && legacy_boolean(&self.enabled).is_some()
            && self.request_timeout_seconds.as_ref().is_none_or(|value| {
                self.kind == "model"
                    && value
                        .as_f64()
                        .or_else(|| value.as_str()?.trim().parse().ok())
                        .is_some_and(|value| (0.05..=300.0).contains(&value))
            })
            && self.config.len() <= 32
            && self
                .config
                .values()
                .all(|value| value.is_boolean() || value.is_number() || value.is_string())
            && self.secret_refs.len() <= 16
            && self.secret_refs.iter().all(|(name, reference)| {
                identifier(name, 64, u8::is_ascii_lowercase, b"_")
                    && reference
                        .strip_prefix("GA_PLUGIN_")
                        .is_some_and(|suffix| identifier(suffix, 128, u8::is_ascii_uppercase, b"_"))
            })
    }
}

fn enabled_default() -> Value {
    Value::Bool(true)
}

fn identifier(text: &str, max: usize, letter: fn(&u8) -> bool, punctuation: &[u8]) -> bool {
    let mut bytes = text.bytes();
    text.len() <= max
        && bytes.next().is_some_and(|first| letter(&first))
        && bytes.all(|byte| letter(&byte) || byte.is_ascii_digit() || punctuation.contains(&byte))
}
