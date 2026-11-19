mod form;
pub use form::{FormProjectionError, form_schema_flags, project_form_fields};

mod custom;
mod entry;
mod specification;
mod tracker_setup;
pub use entry::{CustomEntry, CustomEntryError, ResolvedCustomEntry};
pub use specification::{CustomDefinition, CustomDefinitionError};
pub use tracker_setup::{TrackerSettingsUpdate, TrackerSetupDraft, TrackerSetupError};

pub(crate) use custom::python_equal;
pub use custom::{CustomSchemaError, validate_custom_schema};

use std::{collections::BTreeMap, error::Error, fmt};

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
    let validator = entry_validator(schema)?;
    if validator.is_valid(values) {
        Ok(())
    } else {
        Err(InvalidEntryValues::Contract)
    }
}

fn entry_validator(schema: &Value) -> Result<jsonschema::Validator, InvalidEntryValues> {
    jsonschema::draft202012::options()
        .should_validate_formats(false)
        .build(schema)
        .map_err(|_| InvalidEntryValues::Contract)
}

pub fn tracker_form_issues(
    schema: &Value,
    values: &Value,
) -> Result<Vec<Value>, InvalidEntryValues> {
    use jsonschema::error::ValidationErrorKind;

    let validator = entry_validator(schema)?;
    let mut errors = validator
        .iter_errors(values)
        .map(|error| {
            let schema_path = error.evaluation_path().to_string();
            let instance_path = error.instance_path().to_string();
            (error, schema_path, instance_path)
        })
        .collect::<Vec<_>>();
    errors.sort_by_cached_key(|(_, schema_path, instance_path)| {
        form_error_order(schema, schema_path, instance_path)
    });
    let mut missing = BTreeMap::<_, Vec<_>>::new();
    for (error, schema_path, instance_path) in &errors {
        if let ValidationErrorKind::Required { property } = error.kind() {
            missing
                .entry((schema_path.as_str(), instance_path.as_str()))
                .or_default()
                .push(property);
        }
    }
    for fields in missing.values_mut() {
        fields.sort_by_key(|field| field.as_str());
    }
    let mut issues = Vec::new();
    for (error, schema_path, instance_path) in &errors {
        let issue = match error.kind() {
            ValidationErrorKind::Required { .. } => {
                // Python reports the sorted missing set for every required-property error.
                issues.extend(
                    missing[&(schema_path.as_str(), instance_path.as_str())]
                        .iter()
                        .map(|field| {
                            serde_json::json!({
                                "field":field,"code":"required","message":"This field is required",
                            })
                        }),
                );
                continue;
            }
            ValidationErrorKind::AdditionalProperties { .. } => serde_json::json!({
                "field":"payload","code":"unknown_field","message":"Unknown field",
            }),
            _ => {
                let field = pointer_segments(instance_path)
                    .next()
                    .unwrap_or("payload")
                    .replace("~1", "/")
                    .replace("~0", "~");
                let keyword = schema_path.rsplit('/').next().unwrap_or("schema");
                serde_json::json!({"field":field,"code":keyword,"message":"Invalid field value"})
            }
        };
        issues.push(issue);
    }
    Ok(issues)
}

fn pointer_segments(path: &str) -> impl Iterator<Item = &str> {
    path.split('/').skip(1)
}

fn form_error_order(schema: &Value, schema_path: &str, instance_path: &str) -> Vec<usize> {
    // The reference walks the stored schema in its JSON object order and
    // iterates each array item before proceeding to the next.
    let mut order = Vec::new();
    let mut node = Some(schema);
    let mut instance = pointer_segments(instance_path);
    let mut parent = "";
    for segment in pointer_segments(schema_path) {
        let key = segment.replace("~1", "/").replace("~0", "~");
        if let Some(fields) = node.and_then(Value::as_object) {
            order.push(
                fields
                    .keys()
                    .position(|field| field == &key)
                    .unwrap_or(fields.len()),
            );
            node = fields.get(&key);
        } else if let Ok(index) = key.parse::<usize>() {
            order.push(index);
            node = node.and_then(|value| value.get(index));
        }
        match (parent, segment) {
            ("properties" | "prefixItems", _) => {
                instance.next();
            }
            (_, "items") => {
                if let Some(index) = instance.next().and_then(|index| index.parse().ok()) {
                    order.push(index);
                }
            }
            (_, "$ref") => {
                node = node
                    .and_then(Value::as_str)
                    .and_then(|reference| reference.strip_prefix('#'))
                    .and_then(|pointer| schema.pointer(pointer));
            }
            _ => {}
        }
        parent = segment;
    }
    order
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
