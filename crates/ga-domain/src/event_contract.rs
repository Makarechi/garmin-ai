use std::{error::Error, fmt};

use serde_json::Value;

use crate::{
    legacy_json::{Utf8Layout, encode_ascii},
    sha256_hex,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidContract;

impl fmt::Display for InvalidContract {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Diary definition contract cannot be hashed")
    }
}
impl Error for InvalidContract {}

/// Hash the stored definition format, including the original ASCII escaping.
pub fn definition_hash(contract: &Value) -> Result<String, InvalidContract> {
    let mut normalized = contract.clone();
    let object = normalized.as_object_mut().ok_or(InvalidContract)?;
    if let Some(allowed) = object.get_mut("allowed_operations") {
        let operations = allowed.as_array_mut().ok_or(InvalidContract)?;
        if operations.iter().any(|value| !value.is_string()) {
            return Err(InvalidContract);
        }
        operations.sort_by(|left, right| left.as_str().cmp(&right.as_str()));
    }
    if object.get("derived_duration") == Some(&Value::Bool(false)) {
        object.remove("derived_duration");
    }
    if let Some(fields) = object.get_mut("fields") {
        let fields = fields.as_object_mut().ok_or(InvalidContract)?;
        for field in fields.values_mut() {
            let field = field.as_object_mut().ok_or(InvalidContract)?;
            if field.get("metric_semantics") == Some(&Value::Null) {
                field.remove("metric_semantics");
            }
        }
    }
    Ok(sha256_hex(python_ascii_json(&normalized)?.as_bytes()))
}

/// Hash a metric definition in the stored contract format.
pub fn metric_definition_hash(contract: &Value) -> Result<String, InvalidContract> {
    let mut normalized = contract.clone();
    let object = normalized.as_object_mut().ok_or(InvalidContract)?;
    if object.get("category_domain") == Some(&Value::Null) {
        object.remove("category_domain");
    }
    if let Some(methods) = object.get_mut("allowed_methods") {
        let methods = methods.as_array_mut().ok_or(InvalidContract)?;
        if methods.iter().any(|method| !method.is_string()) {
            return Err(InvalidContract);
        }
        methods.sort_by(|left, right| left.as_str().cmp(&right.as_str()));
    }
    Ok(sha256_hex(python_ascii_json(&normalized)?.as_bytes()))
}

pub(crate) fn python_ascii_json(value: &Value) -> Result<String, InvalidContract> {
    let serialized = serde_json::to_vec(value).map_err(|_| InvalidContract)?;
    encode_ascii(&serialized, Utf8Layout::Compact, serialized.len()).map_err(|_| InvalidContract)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{InvalidContract, definition_hash};

    #[test]
    fn matches_python_sorted_unicode_contract_hash() -> Result<(), InvalidContract> {
        let value = json!({
            "labels": {"en": "Café\u{007f}🧪", "ru": "Дневник"},
            "fields": {"x": {"metric_semantics": null, "unit": "мг"}},
            "derived_duration": false,
            "allowed_operations": ["query", "create"],
            "default": -0.0
        });
        assert_eq!(
            definition_hash(&value)?,
            "2cb03bf48a6d19b0f39a24123d96c4ec8cf6fac125ee647afaf3dc5c72704e2f"
        );
        Ok(())
    }
}
