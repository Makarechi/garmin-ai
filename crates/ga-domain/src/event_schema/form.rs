use std::fmt;

use serde_json::{Map, Value, json};

use super::custom::{ExactNumber, python_equal};

mod minimum;

const CONDITIONAL: &[&str] = &[
    "$ref",
    "oneOf",
    "anyOf",
    "allOf",
    "if",
    "then",
    "else",
    "dependentRequired",
    "dependentSchemas",
];
const SCHEMA_ALTERNATIVES: &[&str] = &["oneOf", "anyOf", "allOf", "if", "then", "else"];
const ANNOTATIONS: &[&str] = &[
    "title",
    "description",
    "default",
    "examples",
    "$comment",
    "deprecated",
    "readOnly",
    "writeOnly",
    "$defs",
];
const DEPTH: usize = 8;

#[derive(Clone, Copy, Debug)]
pub struct FormProjectionError;

impl fmt::Display for FormProjectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Invalid recorded tracker form schema")
    }
}

impl std::error::Error for FormProjectionError {}

fn object(value: &Value) -> Result<&Map<String, Value>, FormProjectionError> {
    value.as_object().ok_or(FormProjectionError)
}

pub fn form_schema_flags(schema: &Value) -> Result<(bool, bool), FormProjectionError> {
    let schema = object(schema)?;
    let mut root = schema.clone();
    root.remove("properties");
    root.remove("$defs");
    let definitions = schema.get("$defs").and_then(Value::as_object);
    Ok((
        schema.contains_key("const") || complex(&Value::Object(root), definitions, 0)?,
        CONDITIONAL
            .iter()
            .any(|&keyword| schema.contains_key(keyword)),
    ))
}

pub fn project_form_fields(
    schema: &Value,
    metadata: &Value,
) -> Result<Vec<Value>, FormProjectionError> {
    let definitions = schema.get("$defs").and_then(Value::as_object);
    let fields = object(&schema["properties"])?;
    // PostgreSQL's stored JSONB object order is length first, then byte order.
    let mut ordered: Vec<_> = fields.iter().collect();
    ordered.sort_by_key(|(name, _)| (name.len(), name.as_str()));
    ordered
        .into_iter()
        .map(|(name, original)| {
            let resolved = resolve(original, definitions)?;
            let node = object(&resolved)?;
            let field = &metadata[name];
            let input = if node.contains_key("enum") {
                "choice"
            } else {
                match node.get("type").and_then(Value::as_str) {
                    Some("integer") => "integer",
                    Some("number") => "number",
                    Some("boolean") => "boolean",
                    Some("string") => "text",
                    _ => "json",
                }
            };
            let required = schema["required"]
                .as_array()
                .is_some_and(|names| names.iter().any(|value| value == name));
            let (lower, lower_exclusive) = width_bound(node, "minimum", "exclusiveMinimum", true)?;
            let (upper, upper_exclusive) = width_bound(node, "maximum", "exclusiveMaximum", false)?;
            let (min_json_length, min_json_storage_length) = if input == "json" {
                (
                    Some(minimum::minimum(original, definitions, 0, false)?),
                    Some(minimum::minimum(&resolved, definitions, 0, true)?),
                )
            } else {
                (None, None)
            };
            let validation_schema = if node.contains_key("enum") || node.contains_key("const") {
                let mut validation = object(original)?.clone();
                let mut defs = validation
                    .remove("$defs")
                    .and_then(|item| item.as_object().cloned())
                    .unwrap_or_default();
                if let Some(definitions) = definitions {
                    defs.extend(definitions.clone());
                }
                validation.insert("$defs".into(), Value::Object(defs));
                Value::Object(validation)
            } else {
                Value::Null
            };
            Ok(json!({
                "name":name,
                "field_id":field["id"].as_str().ok_or(FormProjectionError)?,
                "labels":field["labels"],
                "input":input,"required":required,"unit":field["unit"],
                "minimum":lower,"maximum":upper,
                "exclusive_minimum":lower_exclusive,"exclusive_maximum":upper_exclusive,
                "min_length":node.get("minLength"),"max_length":node.get("maxLength"),
                "min_json_length":min_json_length,"min_json_storage_length":min_json_storage_length,
                "complex_json":complex(original, definitions, 0)?,
                "options":node.get("enum").cloned().unwrap_or_else(|| json!([])),
                "has_const":node.contains_key("const"),"const_value":node.get("const"),
                "validation_schema":validation_schema,
            }))
        })
        .collect()
}

fn referenced<'a>(
    name: &Value,
    definitions: Option<&'a Map<String, Value>>,
) -> Result<&'a Value, FormProjectionError> {
    let target = name
        .as_str()
        .and_then(|name| name.strip_prefix("#/$defs/"))
        .ok_or(FormProjectionError)?;
    definitions
        .and_then(|definitions| definitions.get(target))
        .ok_or(FormProjectionError)
}

fn resolve(
    node: &Value,
    definitions: Option<&Map<String, Value>>,
) -> Result<Value, FormProjectionError> {
    let mut current = object(node)?.clone();
    let mut seen = std::collections::BTreeSet::new();
    while let Some(reference) = current.remove("$ref") {
        if !seen.insert(reference.as_str().ok_or(FormProjectionError)?.to_owned()) {
            return Err(FormProjectionError);
        }
        let original = object(referenced(&reference, definitions)?)?;
        let mut merged = original.clone();
        merged.extend(current.clone());
        for (keyword, largest) in [
            ("minimum", true),
            ("exclusiveMinimum", true),
            ("minLength", true),
            ("maximum", false),
            ("exclusiveMaximum", false),
            ("maxLength", false),
        ] {
            if let (Some(base), Some(sibling)) = (original.get(keyword), current.get(keyword)) {
                let selected = if compares_larger(base, sibling)? == largest {
                    base
                } else {
                    sibling
                };
                merged.insert(keyword.into(), selected.clone());
            }
        }
        if let (Some(base), Some(sibling)) = (
            original.get("enum").and_then(Value::as_array),
            current.get("enum").and_then(Value::as_array),
        ) {
            let allowed: Vec<_> = base
                .iter()
                .filter(|item| {
                    sibling
                        .iter()
                        .any(|candidate| python_equal(item, candidate))
                })
                .collect();
            merged.insert("enum".into(), json!(allowed));
        }
        current = merged;
    }
    Ok(Value::Object(current))
}

fn compares_larger(left: &Value, right: &Value) -> Result<bool, FormProjectionError> {
    Ok(ExactNumber::read(left).ok_or(FormProjectionError)?
        >= ExactNumber::read(right).ok_or(FormProjectionError)?)
}

fn width_bound<'a>(
    node: &'a Map<String, Value>,
    inclusive: &str,
    exclusive: &str,
    lower: bool,
) -> Result<(Option<&'a Value>, bool), FormProjectionError> {
    match (node.get(inclusive), node.get(exclusive)) {
        (Some(closed), Some(open)) => {
            if if lower {
                compares_larger(open, closed)?
            } else {
                compares_larger(closed, open)?
            } {
                Ok((Some(open), true))
            } else {
                Ok((Some(closed), false))
            }
        }
        (_, Some(bound)) => Ok((Some(bound), true)),
        (bound, None) => Ok((bound, false)),
    }
}

fn complex(
    node: &Value,
    definitions: Option<&Map<String, Value>>,
    depth: usize,
) -> Result<bool, FormProjectionError> {
    if depth > DEPTH {
        return Ok(true);
    }
    if let Some(children) = node.as_array() {
        for child in children {
            if complex(child, definitions, depth + 1)? {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    let Some(node) = node.as_object() else {
        return Ok(false);
    };
    if SCHEMA_ALTERNATIVES
        .iter()
        .any(|&keyword| node.contains_key(keyword))
    {
        return Ok(true);
    }
    if let Some(reference) = node.get("$ref")
        && (node
            .keys()
            .any(|key| key != "$ref" && !ANNOTATIONS.contains(&key.as_str()))
            || complex(referenced(reference, definitions)?, definitions, depth + 1)?)
    {
        return Ok(true);
    }
    for (key, value) in node {
        if key == "$ref" || ANNOTATIONS.contains(&key.as_str()) {
            continue;
        }
        if key == "properties" {
            for child in object(value)?.values() {
                if complex(child, definitions, depth + 1)? {
                    return Ok(true);
                }
            }
        } else if complex(value, definitions, depth + 1)? {
            return Ok(true);
        }
    }
    Ok(false)
}
