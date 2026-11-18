use std::collections::BTreeSet;

use serde_json::{Map, Value, json};

use crate::legacy_json::{Utf8Layout, encode, encode_ascii};

use super::{DEPTH, FormProjectionError, object, referenced};

mod number;

fn serialized_length(value: &Value, storage: bool) -> Result<usize, FormProjectionError> {
    const INPUT_BYTES: usize = 512_000;
    let raw = serde_json::to_vec(value).map_err(|_| FormProjectionError)?;
    if storage {
        return encode_ascii(&raw, Utf8Layout::Compact, INPUT_BYTES)
            .map(|text| text.len())
            .map_err(|_| FormProjectionError);
    }
    let raw = encode(&raw, Utf8Layout::Compact, INPUT_BYTES).map_err(|_| FormProjectionError)?;
    std::str::from_utf8(&raw)
        .map(|text| text.encode_utf16().count())
        .map_err(|_| FormProjectionError)
}

pub(super) fn minimum(
    value: &Value,
    definitions: Option<&Map<String, Value>>,
    depth: usize,
    storage: bool,
) -> Result<usize, FormProjectionError> {
    if depth > DEPTH {
        return Ok(0);
    }
    let node = object(value)?;
    let visit = |value| minimum(value, definitions, depth + 1, storage);
    if let Some(reference) = node.get("$ref") {
        let reference = referenced(reference, definitions)?;
        let mut siblings = node.clone();
        siblings.remove("$ref");
        if reference["type"] == "object" && siblings.get("type").is_none_or(|kind| kind == "object")
        {
            let mut properties = reference["properties"]
                .as_object()
                .cloned()
                .unwrap_or_default();
            if let Some(additional) = siblings.get("properties").and_then(Value::as_object) {
                for (key, value) in additional {
                    let combined = properties
                        .remove(key)
                        .map_or_else(|| value.clone(), |prior| json!({"allOf":[prior,value]}));
                    properties.insert(key.clone(), combined);
                }
            }
            let required: BTreeSet<_> = [reference.get("required"), siblings.get("required")]
                .into_iter()
                .flatten()
                .filter_map(Value::as_array)
                .flatten()
                .filter_map(Value::as_str)
                .collect();
            let mut merged = object(reference)?.clone();
            merged.extend(siblings.clone());
            merged.insert("properties".into(), Value::Object(properties));
            merged.insert("required".into(), json!(required));
            return visit(&Value::Object(merged));
        }
        return Ok(visit(reference)?.max(visit(&Value::Object(siblings))?));
    }
    if let Some(value) = node.get("const") {
        return serialized_length(value, storage);
    }
    if let Some(values) = node.get("enum").and_then(Value::as_array) {
        return values
            .iter()
            .map(|value| serialized_length(value, storage))
            .try_fold(usize::MAX, |shortest, length| Ok(shortest.min(length?)));
    }
    let mut size = match node.get("type").and_then(Value::as_str) {
        Some("string") => 2 + bound(node, "minLength")?,
        Some("array") => {
            let count = bound(node, "minItems")?;
            let item = node.get("items").ok_or(FormProjectionError)?;
            2usize
                .saturating_add(count.saturating_mul(visit(item)?))
                .saturating_add(count.saturating_sub(1))
        }
        Some("object") => {
            let required = node
                .get("required")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or_default();
            let mut sum = 2usize + required.len().saturating_sub(1);
            for name in required {
                let key = name.as_str().ok_or(FormProjectionError)?;
                let child = node
                    .get("properties")
                    .and_then(|properties| properties.get(key))
                    .ok_or(FormProjectionError)?;
                sum = sum
                    .saturating_add(serialized_length(name, storage)?)
                    .saturating_add(1)
                    .saturating_add(visit(child)?);
            }
            sum
        }
        Some("null" | "boolean") => 4,
        Some("integer") => number::integer(node, true, storage)?,
        Some("number") => number::number(node, storage)?,
        _ => 1,
    };
    for keyword in ["oneOf", "anyOf", "allOf"] {
        if let Some(schemas) = node.get(keyword).and_then(Value::as_array) {
            let mut lengths = schemas.iter().map(visit);
            let first = lengths.next().ok_or(FormProjectionError)??;
            let combined = lengths.try_fold(first, |current, next| {
                next.map(|next| {
                    if keyword == "allOf" {
                        current.max(next)
                    } else {
                        current.min(next)
                    }
                })
            })?;
            size = size.max(combined);
        }
    }
    Ok(size)
}

fn bound(node: &Map<String, Value>, key: &str) -> Result<usize, FormProjectionError> {
    node.get(key)
        .map(|value| {
            value
                .as_u64()
                .and_then(|value| usize::try_from(value).ok())
                .ok_or(FormProjectionError)
        })
        .transpose()
        .map(|value| value.unwrap_or(0))
}
