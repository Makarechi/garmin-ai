mod custom;
mod specification;
pub use specification::{CustomDefinition, CustomDefinitionError};

pub use custom::{CustomSchemaError, validate_custom_schema};

use std::{error::Error, fmt};

use serde_json::Value;

use crate::event_contract::python_ascii_json;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvalidEntryValues {
    Limits,
    Contract,
}

impl fmt::Display for InvalidEntryValues {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Limits => "Diary entry values exceed their storage limits",
            Self::Contract => "Diary entry values do not match the recorded definition",
        })
    }
}

impl Error for InvalidEntryValues {}

/// Match the existing value profile and its recorded Draft 2020-12 schema.
pub fn validate_entry_values(schema: &Value, values: &Value) -> Result<(), InvalidEntryValues> {
    check_value_limits(values, 0)?;
    let encoded = python_ascii_json(values).map_err(|_| InvalidEntryValues::Limits)?;
    if encoded.len() > 65_536 {
        return Err(InvalidEntryValues::Limits);
    }
    let validator = jsonschema::draft202012::options()
        .should_validate_formats(false)
        .build(schema)
        .map_err(|_| InvalidEntryValues::Contract)?;
    if validator.is_valid(values) {
        Ok(())
    } else {
        Err(InvalidEntryValues::Contract)
    }
}

fn check_value_limits(value: &Value, depth: u8) -> Result<(), InvalidEntryValues> {
    if depth > 8 {
        return Err(InvalidEntryValues::Limits);
    }
    match value {
        Value::Object(fields) => {
            if fields.len() > 32 || fields.keys().any(|key| key.chars().count() > 64) {
                return Err(InvalidEntryValues::Limits);
            }
            for value in fields.values() {
                check_value_limits(value, depth + 1)?;
            }
        }
        Value::Array(items) => {
            if items.len() > 1000 {
                return Err(InvalidEntryValues::Limits);
            }
            for item in items {
                check_value_limits(item, depth + 1)?;
            }
        }
        Value::String(string) if string.chars().count() > 16_000 => {
            return Err(InvalidEntryValues::Limits);
        }
        _ => (),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn validates_local_references_numeric_types_and_ascii_storage_limit() {
        let schema = json!({
            "type": "object",
            "required": ["count"],
            "properties": {"count": {"$ref": "#/$defs/positive"}},
            "$defs": {"positive": {"type": "integer", "minimum": 1}},
            "additionalProperties": false
        });
        for count in [json!(1), json!(1.0)] {
            assert_eq!(
                validate_entry_values(&schema, &json!({"count": count})),
                Ok(())
            );
        }
        for count in [json!(true), json!(0), json!(1.5)] {
            assert_eq!(
                validate_entry_values(&schema, &json!({"count": count})),
                Err(InvalidEntryValues::Contract)
            );
        }
        assert_eq!(
            validate_entry_values(&json!(true), &json!("🧪".repeat(5461))),
            Ok(())
        );
        assert_eq!(
            validate_entry_values(&json!(true), &json!("🧪".repeat(5462))),
            Err(InvalidEntryValues::Limits)
        );
        assert_eq!(
            validate_entry_values(
                &json!({"$ref": "file:///unavailable-schema.json"}),
                &json!({})
            ),
            Err(InvalidEntryValues::Contract)
        );
    }
}
