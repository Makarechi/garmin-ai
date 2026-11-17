use std::{error::Error, fmt, fmt::Write};

use serde_json::Value;

use crate::{
    legacy_json::{Utf8Layout, encode},
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
    let serialized = serde_json::to_vec(&normalized).map_err(|_| InvalidContract)?;
    let compact =
        encode(&serialized, Utf8Layout::Compact, serialized.len()).map_err(|_| InvalidContract)?;
    let compact = str::from_utf8(&compact).map_err(|_| InvalidContract)?;
    let mut ascii = String::with_capacity(compact.len());
    for character in compact.chars() {
        if character < '\u{007f}' {
            ascii.push(character);
        } else {
            for unit in character.encode_utf16(&mut [0; 2]) {
                write!(ascii, "\\u{unit:04x}").map_err(|_| InvalidContract)?;
            }
        }
    }
    Ok(sha256_hex(ascii.as_bytes()))
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
