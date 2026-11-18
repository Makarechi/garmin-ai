use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};

use crate::{
    event_payload::python_whitespace,
    event_schema::{TrackerSetupDraft, TrackerSetupError},
    legacy_json::{Utf8Layout, encode, encode_ascii},
    metric_contract::methods_for,
    sha256_hex,
};

pub struct CommunityPack {
    key: String,
    version: i64,
    document: Value,
    trackers: Vec<TrackerSetupDraft>,
    hash: String,
}

impl CommunityPack {
    pub fn from_json(value: &Value, known_packs: &[&str]) -> Result<Self, TrackerSetupError> {
        use TrackerSetupError::Invalid;
        let object = value
            .as_object()
            .filter(|object| {
                object.keys().all(|key| {
                    matches!(
                        key.as_str(),
                        "format"
                            | "key"
                            | "version"
                            | "title"
                            | "description"
                            | "required_packs"
                            | "trackers"
                            | "analysis"
                            | "limitations"
                    )
                })
            })
            .ok_or(Invalid)?;
        if object
            .get("format")
            .is_some_and(|format| format != "garmin-ai-community-pack-v1")
        {
            return Err(Invalid);
        }
        let key = text(&value["key"], 1, 63)?;
        let mut bytes = key.bytes();
        if !bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
            || !bytes.all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_-".contains(&byte)
            })
        {
            return Err(Invalid);
        }
        let version = value["version"]
            .as_i64()
            .filter(|version| (1..=1000).contains(version))
            .ok_or(Invalid)?;
        let title = text(&value["title"], 1, 120)?;
        let description = text(&value["description"], 1, 1000)?;
        let required = strings(object.get("required_packs"), 6)?;
        let unique = required.iter().copied().collect::<BTreeSet<_>>();
        if unique.len() != required.len() || unique.iter().any(|key| !known_packs.contains(key)) {
            return Err(Invalid);
        }
        let definitions = value["trackers"]
            .as_array()
            .filter(|rows| (1..=8).contains(&rows.len()))
            .ok_or(Invalid)?;
        let trackers = definitions
            .iter()
            .map(TrackerSetupDraft::from_json)
            .collect::<Result<Vec<_>, _>>()?;
        let mut keys = BTreeSet::new();
        let mut metrics = BTreeMap::from([
            ("system.sleep_score".to_owned(), "ordinal"),
            ("system.training_readiness_score".to_owned(), "ordinal"),
        ]);
        for tracker in &trackers {
            let draft = tracker.document();
            let key = draft["key"].as_str().ok_or(Invalid)?;
            if !keys.insert(key.to_owned())
                || draft["reminder_enabled"] == true
                || !draft["reminder_time"].is_null()
                || draft["privacy"] != "sensitive"
                || draft["shortcut"]
                    .as_str()
                    .is_some_and(|text| text.chars().all(python_whitespace))
            {
                return Err(Invalid);
            }
            for field in draft["fields"].as_array().ok_or(Invalid)? {
                let kind = match field["kind"].as_str() {
                    Some("scale") => "ordinal",
                    Some("choice") => "nominal",
                    Some("boolean") => "boolean",
                    Some("integer" | "number") => "physical_number",
                    _ => continue,
                };
                let kind = match field["metric_semantics"].as_str() {
                    Some("event_total" | "event_count") => "increment",
                    Some("interval_total") => "interval_total",
                    Some("cumulative_counter") => "cumulative_counter",
                    _ => kind,
                };
                let field_key = field["key"].as_str().ok_or(Invalid)?;
                metrics.insert(format!("user.{key}.{field_key}"), kind);
            }
            if draft["derived_duration"] == true {
                metrics.insert(format!("user.{key}.elapsed_minutes"), "interval_total");
            }
        }
        let recipes = match object.get("analysis") {
            None => Vec::new(),
            Some(Value::Array(rows)) if rows.len() <= 8 => rows
                .iter()
                .map(|recipe| recipe_document(recipe, &metrics, &unique))
                .collect::<Result<Vec<_>, _>>()?,
            _ => return Err(Invalid),
        };
        let limitations = strings(object.get("limitations"), 8)?;
        let document = json!({
            "format":"garmin-ai-community-pack-v1", "key":key, "version":version,
            "title":title, "description":description, "required_packs":required,
            "trackers":trackers.iter().map(TrackerSetupDraft::document).collect::<Vec<_>>(),
            "analysis":recipes, "limitations":limitations,
        });
        let supplied = supplied_fields(&document, value);
        let raw = serde_json::to_vec(&supplied).map_err(|_| Invalid)?;
        if encode(&raw, Utf8Layout::Compact, 1024 * 1024)
            .map_err(|_| Invalid)?
            .len()
            > 64_000
        {
            return Err(Invalid);
        }
        let wire = serde_json::to_vec(&document).map_err(|_| Invalid)?;
        let ascii = encode_ascii(&wire, Utf8Layout::Compact, 1024 * 1024).map_err(|_| Invalid)?;
        let hash = sha256_hex(ascii.as_bytes());
        Ok(Self {
            key: key.to_owned(),
            version,
            document,
            trackers,
            hash,
        })
    }

    pub fn document(&self) -> &Value {
        &self.document
    }
    pub fn trackers(&self) -> &[TrackerSetupDraft] {
        &self.trackers
    }
    pub fn hash(&self) -> &str {
        &self.hash
    }
    pub fn key(&self) -> &str {
        &self.key
    }
    pub fn version(&self) -> i64 {
        self.version
    }
}

fn recipe_document(
    value: &Value,
    metrics: &BTreeMap<String, &str>,
    packs: &BTreeSet<&str>,
) -> Result<Value, TrackerSetupError> {
    use TrackerSetupError::Invalid;
    let object = value
        .as_object()
        .filter(|object| {
            object.keys().all(|key| {
                matches!(
                    key.as_str(),
                    "label" | "operation" | "metric_key" | "method" | "limitation"
                )
            })
        })
        .ok_or(Invalid)?;
    let label = text(&value["label"], 1, 120)?;
    let limitation = text(&value["limitation"], 1, 500)?;
    let operation = text(&value["operation"], 1, 100)?;
    let metric = text(&value["metric_key"], 1, 134)?;
    let required = match metric {
        "system.sleep_score" => Some("sleep"),
        "system.training_readiness_score" => Some("training"),
        _ => None,
    };
    if required.is_some_and(|pack| !packs.contains(pack)) {
        return Err(Invalid);
    }
    let kind = metrics.get(metric).ok_or(Invalid)?;
    let method = object
        .get("method")
        .filter(|value| !value.is_null())
        .map(|method| text(method, 1, 100))
        .transpose()?;
    match (operation, method) {
        ("query_observations", None) => {}
        ("aggregate_metric" | "compare_periods", Some(method))
            if methods_for(kind).is_some_and(|allowed| allowed.contains(&method)) => {}
        _ => return Err(Invalid),
    }
    Ok(
        json!({"label":label,"operation":operation,"metric_key":metric,"method":method,"limitation":limitation}),
    )
}

fn text(value: &Value, min: usize, max: usize) -> Result<&str, TrackerSetupError> {
    value
        .as_str()
        .filter(|text| (min..=max).contains(&text.chars().count()))
        .ok_or(TrackerSetupError::Invalid)
}

fn strings(value: Option<&Value>, max: usize) -> Result<Vec<&str>, TrackerSetupError> {
    match value {
        None => Ok(Vec::new()),
        Some(Value::Array(rows)) if rows.len() <= max => rows
            .iter()
            .map(|row| row.as_str().ok_or(TrackerSetupError::Invalid))
            .collect(),
        _ => Err(TrackerSetupError::Invalid),
    }
}

fn supplied_fields(normalized: &Value, supplied: &Value) -> Value {
    match (normalized, supplied) {
        (Value::Object(normalized), Value::Object(supplied)) => Value::Object(
            supplied
                .keys()
                .filter_map(|key| {
                    normalized
                        .get(key)
                        .map(|value| (key.clone(), supplied_fields(value, &supplied[key])))
                })
                .collect::<Map<_, _>>(),
        ),
        (Value::Array(normalized), Value::Array(supplied)) => Value::Array(
            normalized
                .iter()
                .zip(supplied)
                .map(|(value, supplied)| supplied_fields(value, supplied))
                .collect(),
        ),
        _ => normalized.clone(),
    }
}
