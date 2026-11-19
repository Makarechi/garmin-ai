use std::collections::BTreeSet;

use ga_app::garmin_worker::garmin_auth_url;
use ga_domain::{
    event_payload::{SystemPayload, python_whitespace},
    timestamp::StoredInstant,
};
use serde::{Deserialize, de::DeserializeOwned};
use serde_json::Value;
use uuid::Uuid;

use super::{INVALID_SETTINGS, ValueMap};

pub(super) fn validate(values: &ValueMap) -> Result<(), &'static str> {
    if values
        .get("GA_MODEL_REQUEST_TIMEOUT_SECONDS")
        .is_some_and(|timeout| {
            !timeout
                .parse::<f64>()
                .is_ok_and(|seconds| (0.05..=300.0).contains(&seconds))
        })
        || values
            .get("GA_TELEGRAM_DISPATCHER_VERSION")
            .is_some_and(|version| !matches!(version.as_str(), "legacy-v1" | "neutral-shadow-v1"))
        || values
            .get("GA_GARMIN_AUTH_URL")
            .is_some_and(|url| !url.is_empty() && garmin_auth_url(url).is_none())
    {
        return Err(INVALID_SETTINGS);
    }

    let models = bounded_array::<String>(values, "GA_GEMINI_FALLBACK_MODELS", 8)?;
    if models
        .iter()
        .any(|model| !(1..=200).contains(&model.chars().count()))
    {
        return Err(INVALID_SETTINGS);
    }
    let calendar = bounded_array::<CalendarSource>(values, "GA_CALENDAR_SOURCES", 32)?;
    if calendar.iter().any(|source| !source.valid()) {
        return Err(INVALID_SETTINGS);
    }
    let presets = bounded_array::<CaffeinePreset>(values, "GA_CAFFEINE_PRESETS", 12)?;
    let mut identities = BTreeSet::new();
    let mut labels = BTreeSet::new();
    for preset in presets {
        let id = Uuid::parse_str(&preset.id).map_err(|_| INVALID_SETTINGS)?;
        if !identities.insert(id) || !labels.insert(preset.label_key().ok_or(INVALID_SETTINGS)?) {
            return Err(INVALID_SETTINGS);
        }
    }
    Ok(())
}

fn bounded_array<T: DeserializeOwned>(
    values: &ValueMap,
    key: &str,
    limit: usize,
) -> Result<Vec<T>, &'static str> {
    values.get(key).map_or_else(
        || Ok(Vec::new()),
        |value| {
            serde_json::from_str::<Vec<T>>(value)
                .ok()
                .filter(|items| items.len() <= limit)
                .ok_or(INVALID_SETTINGS)
        },
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CalendarSource {
    id: String,
    categories: BTreeSet<String>,
    granted_at: Value,
}

impl CalendarSource {
    fn valid(&self) -> bool {
        Uuid::parse_str(&self.id).is_ok()
            && !self.categories.is_empty()
            && self.categories.iter().all(|category| {
                matches!(
                    category.as_str(),
                    "work" | "personal" | "travel" | "exercise" | "other"
                )
            })
            && StoredInstant::from_legacy_input(&self.granted_at).is_some()
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CaffeinePreset {
    id: String,
    name: String,
    recipe: serde_json::Map<String, Value>,
}

impl CaffeinePreset {
    fn label_key(self) -> Option<(String, String)> {
        if self.name.chars().all(python_whitespace) || self.name.chars().count() > 100 {
            return None;
        }
        let mut recipe = self.recipe;
        recipe.entry("type").or_insert(Value::from("caffeine"));
        let validated = SystemPayload::from_value(Value::Object(recipe)).ok()?;
        if validated.kind() != "caffeine" {
            return None;
        }
        let recipe = validated.json();
        let factor = match recipe["dose_basis"].as_str()? {
            "total" => Some(1.0),
            "per_serving" => recipe["servings"].as_f64(),
            _ => None,
        };
        // Python's preset buttons display six significant digits. Use the same
        // rounding for equality even where the label uses decimal notation.
        let amount = |field: &str| -> Option<String> {
            Some(format!("{:.5e}", recipe[field].as_f64()? * factor?))
        };
        let estimate = amount("caffeine_mg_estimate");
        let low = amount("caffeine_mg_min");
        let high = amount("caffeine_mg_max");
        let dose = match (low, estimate, high) {
            (Some(low), _, Some(high)) => format!("range:{low}:{high}"),
            (_, Some(value), _) if recipe["dose_provenance"] == "reported_label" => value,
            (_, Some(value), _) => format!("estimate:{value}"),
            (Some(low), _, _) => format!("minimum:{low}"),
            (_, _, Some(high)) => format!("maximum:{high}"),
            _ => String::new(),
        };
        Some((self.name.chars().take(50).collect(), dose))
    }
}
