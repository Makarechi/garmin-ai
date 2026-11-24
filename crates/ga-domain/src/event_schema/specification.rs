use std::{collections::BTreeSet, error::Error, fmt};

use serde_json::{Map, Value, json};

use crate::{
    event_contract::definition_hash,
    event_payload::{boolean, python_whitespace},
    language_tags::normalize_language_tag,
};

use super::{CustomSchemaError, custom::field_name, validate_custom_schema};

#[derive(Debug)]
pub enum CustomDefinitionError {
    Shape,
    Key,
    Labels,
    Fields,
    Operations,
    Semantics,
    Schema(CustomSchemaError),
}

impl fmt::Display for CustomDefinitionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Shape => "Custom diary definition has invalid fields",
            Self::Key => "Custom diary definition key is invalid",
            Self::Labels => "Custom diary labels are missing, invalid or duplicated",
            Self::Fields => {
                "Field metadata must match the schema and have distinct stable identities"
            }
            Self::Operations => "Custom diary definition permissions are invalid",
            Self::Semantics => "Custom diary field semantics conflict with the schema",
            Self::Schema(error) => return error.fmt(f),
        })
    }
}

impl Error for CustomDefinitionError {}

pub struct CustomDefinition {
    key: String,
    contract: Value,
    hash: String,
}

impl CustomDefinition {
    /// Match the Python custom-definition input and its normalized storage contract.
    pub fn from_json(document: &Value) -> Result<Self, CustomDefinitionError> {
        let input = document.as_object().ok_or(CustomDefinitionError::Shape)?;
        if input.keys().any(|key| {
            !matches!(
                key.as_str(),
                "key"
                    | "labels"
                    | "schema"
                    | "payload_schema"
                    | "fields"
                    | "topology"
                    | "derived_duration"
                    | "privacy"
                    | "allowed_operations"
            )
        }) {
            return Err(CustomDefinitionError::Shape);
        }
        let key = input
            .get("key")
            .and_then(Value::as_str)
            .filter(|key| key.strip_prefix("user.").is_some_and(field_name))
            .ok_or(CustomDefinitionError::Key)?;
        let topology = input
            .get("topology")
            .and_then(Value::as_str)
            .filter(|topology| {
                matches!(
                    *topology,
                    "point" | "open_interval" | "bounded_interval" | "flexible"
                )
            })
            .ok_or(CustomDefinitionError::Shape)?;
        let ((Some(schema), None) | (None, Some(schema))) =
            (input.get("schema"), input.get("payload_schema"))
        else {
            return Err(CustomDefinitionError::Shape);
        };
        validate_custom_schema(schema).map_err(CustomDefinitionError::Schema)?;
        let properties = schema["properties"]
            .as_object()
            .ok_or(CustomDefinitionError::Fields)?;
        let fields = input
            .get("fields")
            .and_then(Value::as_object)
            .ok_or(CustomDefinitionError::Fields)?;
        if fields.is_empty()
            || fields.len() > 32
            || fields.len() != properties.len()
            || properties.contains_key("type")
        {
            return Err(CustomDefinitionError::Fields);
        }
        let privacy = input
            .get("privacy")
            .map_or(Some("private"), Value::as_str)
            .filter(|privacy| matches!(*privacy, "private" | "sensitive"))
            .ok_or(CustomDefinitionError::Shape)?;
        let derived = input
            .get("derived_duration")
            .map_or(Some(false), |value| {
                boolean(value.clone()).and_then(|value| value.as_bool())
            })
            .ok_or(CustomDefinitionError::Shape)?;
        if derived && topology == "point" {
            return Err(CustomDefinitionError::Semantics);
        }
        let duration_id = format!("{key}.elapsed_minutes");
        let mut metadata = Map::new();
        let mut known = BTreeSet::new();
        for (name, field) in fields {
            let property = properties.get(name).ok_or(CustomDefinitionError::Fields)?;
            let field = field.as_object().ok_or(CustomDefinitionError::Fields)?;
            let normalized = normalize_field(field)?;
            let stable = normalized["id"]
                .as_str()
                .ok_or(CustomDefinitionError::Fields)?;
            if !known.insert(stable.to_owned()) {
                return Err(CustomDefinitionError::Fields);
            }
            let semantics = normalized["metric_semantics"].as_str();
            if (derived && stable == duration_id)
                || (topology != "bounded_interval" && semantics == Some("interval_total"))
                || (semantics == Some("event_count")
                    && !integer_or_null(property, &schema["$defs"]))
            {
                return Err(CustomDefinitionError::Semantics);
            }
            metadata.insert(name.clone(), normalized);
        }
        let operations = normalize_operations(input.get("allowed_operations"))?;
        let labels = normalize_labels(input.get("labels").ok_or(CustomDefinitionError::Labels)?)?;
        let contract = json!({
            "key": key, "labels": labels, "schema": schema, "fields": metadata, "topology": topology,
            "derived_duration": derived, "privacy": privacy, "allowed_operations": operations,
        });
        let hash = definition_hash(&contract).map_err(|_| CustomDefinitionError::Shape)?;
        Ok(Self {
            key: key.to_owned(),
            contract,
            hash,
        })
    }

    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }

    #[must_use]
    pub fn contract(&self) -> &Value {
        &self.contract
    }

    #[must_use]
    pub fn hash(&self) -> &str {
        &self.hash
    }
}

fn normalize_operations(declared: Option<&Value>) -> Result<BTreeSet<&str>, CustomDefinitionError> {
    let Some(declared) = declared else {
        return Ok(BTreeSet::from(["create", "delete", "query", "update"]));
    };
    let list = declared
        .as_array()
        .filter(|list| !list.is_empty())
        .ok_or(CustomDefinitionError::Operations)?;
    list.iter()
        .map(|value| {
            value
                .as_str()
                .filter(|name| matches!(*name, "create" | "delete" | "query" | "update"))
                .ok_or(CustomDefinitionError::Operations)
        })
        .collect()
}

fn normalize_field(field: &Map<String, Value>) -> Result<Value, CustomDefinitionError> {
    if field.keys().any(|key| {
        !matches!(
            key.as_str(),
            "id" | "labels" | "semantic" | "unit" | "metric_semantics"
        )
    }) {
        return Err(CustomDefinitionError::Fields);
    }
    let id = field
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| stable_id(id))
        .ok_or(CustomDefinitionError::Fields)?;
    let labels = normalize_labels(field.get("labels").ok_or(CustomDefinitionError::Labels)?)?;
    let semantic = field
        .get("semantic")
        .and_then(Value::as_str)
        .filter(|name| {
            matches!(
                *name,
                "nominal" | "ordinal" | "count" | "quantity" | "text" | "boolean"
            )
        })
        .ok_or(CustomDefinitionError::Semantics)?;
    let unit = field
        .get("unit")
        .filter(|unit| !unit.is_null())
        .map(|unit| {
            unit.as_str()
                .filter(|unit| {
                    (1..=32).contains(&unit.len())
                        && unit.bytes().all(|byte| {
                            byte.is_ascii_alphanumeric()
                                || matches!(byte, b'_' | b'%' | b'.' | b'/' | b'-')
                        })
                })
                .ok_or(CustomDefinitionError::Semantics)
        })
        .transpose()?;
    let metric = field
        .get("metric_semantics")
        .filter(|metric| !metric.is_null())
        .map(|metric| {
            metric
                .as_str()
                .filter(|name| {
                    matches!(
                        *name,
                        "gauge"
                            | "event_total"
                            | "event_count"
                            | "interval_total"
                            | "cumulative_counter"
                    )
                })
                .ok_or(CustomDefinitionError::Semantics)
        })
        .transpose()?;
    if (metric.is_some() && !matches!(semantic, "count" | "quantity"))
        || (metric == Some("event_count")
            && (semantic != "count" || !matches!(unit, None | Some("count" | "steps"))))
    {
        return Err(CustomDefinitionError::Semantics);
    }
    Ok(
        json!({"id": id, "labels": labels, "semantic": semantic, "unit": unit, "metric_semantics": metric}),
    )
}

fn stable_id(id: &str) -> bool {
    let mut chars = id.bytes();
    id.len() <= 128
        && chars.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && chars.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'.' | b'-')
        })
}

fn normalize_labels(value: &Value) -> Result<Map<String, Value>, CustomDefinitionError> {
    let labels = value
        .as_object()
        .filter(|labels| (1..=8).contains(&labels.len()))
        .ok_or(CustomDefinitionError::Labels)?;
    let mut normalized = Map::new();
    for (locale, label) in labels {
        let tag = normalize_language_tag(locale).ok_or(CustomDefinitionError::Labels)?;
        let label = label
            .as_str()
            .filter(|label| {
                label.chars().count() <= 120 && !label.trim_matches(python_whitespace).is_empty()
            })
            .ok_or(CustomDefinitionError::Labels)?;
        if normalized.insert(tag, json!(label)).is_some() {
            return Err(CustomDefinitionError::Labels);
        }
    }
    Ok(normalized)
}

fn integer_or_null(schema: &Value, definitions: &Value) -> bool {
    let local_ref = schema["$ref"]
        .as_str()
        .and_then(|uri| uri.strip_prefix("#/$defs/"));
    if local_ref.is_some_and(|reference| !integer_or_null(&definitions[reference], definitions)) {
        return false;
    }
    let branches = ["anyOf", "oneOf"]
        .into_iter()
        .filter_map(|key| schema[key].as_array())
        .flatten();
    let mut composed = false;
    for branch in branches {
        composed = true;
        if !integer_or_null(branch, definitions) {
            return false;
        }
    }
    match schema["type"].as_str() {
        Some("integer" | "null") => true,
        Some(_) => false,
        None if local_ref.is_some() || composed => true,
        None => {
            let literals = schema
                .get("const")
                .map(std::slice::from_ref)
                .or_else(|| schema["enum"].as_array().map(Vec::as_slice));
            literals.is_some_and(|literals| {
                !literals.is_empty()
                    && literals.iter().all(|value| {
                        value.is_null()
                            || value
                                .as_number()
                                .is_some_and(|number| !number.as_str().contains(['.', 'e', 'E']))
                    })
            })
        }
    }
}
