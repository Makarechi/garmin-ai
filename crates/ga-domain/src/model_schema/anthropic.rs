use std::collections::BTreeSet;

use jiter::JsonValue;
use serde_json::Value;

use super::{Fields, LOCAL_REF, ModelSchemaError, array, field, object, text};

const MAX_UNIONS: usize = 16;
const MAX_NULLABLE_REDUCTIONS: usize = 24;

pub(super) fn reject_recursive(definitions: &Fields<'_>) -> Result<(), ModelSchemaError> {
    enum Visit<'a> {
        Enter(&'a str),
        Exit(&'a str),
    }
    let mut pending: Vec<_> = definitions
        .iter()
        .map(|(name, _)| Visit::Enter(name))
        .collect();
    let mut active = BTreeSet::new();
    let mut complete = BTreeSet::new();
    while let Some(visit) = pending.pop() {
        match visit {
            Visit::Enter(name) if !complete.contains(name) => {
                if !active.insert(name) {
                    return Err(ModelSchemaError::RecursiveSchema);
                }
                let definition =
                    field(definitions, name).ok_or(ModelSchemaError::UnsupportedReference)?;
                let mut references = Vec::new();
                local_references(definition, &mut references);
                pending.push(Visit::Exit(name));
                pending.extend(references.into_iter().map(Visit::Enter));
            }
            Visit::Enter(_) => {}
            Visit::Exit(name) => {
                active.remove(name);
                complete.insert(name);
            }
        }
    }
    Ok(())
}

fn local_references<'a>(node: &'a JsonValue<'_>, references: &mut Vec<&'a str>) {
    match node {
        JsonValue::Object(fields) => {
            if let Some(name) =
                text(field(fields, "$ref")).and_then(|reference| reference.strip_prefix(LOCAL_REF))
            {
                references.push(name);
            }
            for (_, value) in fields.iter() {
                local_references(value, references);
            }
        }
        JsonValue::Array(values) => {
            for value in values.iter() {
                local_references(value, references);
            }
        }
        _ => {}
    }
}

pub(super) fn limit_unions(
    root: &Fields<'_>,
    definitions: &Fields<'_>,
    output: &mut Value,
) -> Result<(), ModelSchemaError> {
    let mut remaining = count_unions(output).saturating_sub(MAX_UNIONS);
    if remaining > MAX_NULLABLE_REDUCTIONS {
        return Err(ModelSchemaError::Complexity);
    }
    simplify(root, output, &mut remaining)?;
    for (name, definition) in definitions {
        let fields = object(definition).ok_or(ModelSchemaError::InvalidDocument)?;
        simplify(fields, &mut output["$defs"][name.as_ref()], &mut remaining)?;
    }
    if remaining > 0 || count_unions(output) > MAX_UNIONS {
        return Err(ModelSchemaError::Complexity);
    }
    Ok(())
}

fn count_unions(node: &Value) -> usize {
    match node {
        Value::Array(values) => values.iter().map(count_unions).sum(),
        Value::Object(fields) => {
            let nested: usize = fields
                .iter()
                .map(|(key, value)| {
                    if matches!(key.as_str(), "properties" | "$defs") {
                        value
                            .as_object()
                            .into_iter()
                            .flat_map(|fields| fields.values())
                            .map(count_unions)
                            .sum()
                    } else {
                        count_unions(value)
                    }
                })
                .sum();
            usize::from(fields.contains_key("anyOf")) + nested
        }
        _ => 0,
    }
}

fn simplify(
    source: &Fields<'_>,
    target: &mut Value,
    remaining: &mut usize,
) -> Result<(), ModelSchemaError> {
    if *remaining == 0 {
        return Ok(());
    }
    if let Some(properties) = field(source, "properties").and_then(object) {
        for (name, schema) in properties {
            let fields = object(schema).ok_or(ModelSchemaError::InvalidDocument)?;
            let selected =
                nullable_default(source, name, fields, &target["properties"][name.as_ref()]);
            if *remaining > 0
                && let Some((index, selected)) = selected
            {
                let property = &mut target["properties"][name.as_ref()];
                let projected = property["anyOf"]
                    .as_array_mut()
                    .ok_or(ModelSchemaError::InvalidDocument)?;
                *property = projected.swap_remove(index);
                target["required"]
                    .as_array_mut()
                    .ok_or(ModelSchemaError::InvalidDocument)?
                    .retain(|required| required.as_str() != Some(name));
                *remaining -= 1;
                simplify(
                    selected,
                    &mut target["properties"][name.as_ref()],
                    remaining,
                )?;
            } else {
                simplify(fields, &mut target["properties"][name.as_ref()], remaining)?;
            }
        }
    }
    for keyword in ["anyOf", "oneOf"] {
        if let Some(variants) = field(source, keyword).and_then(array) {
            let projected = target["anyOf"]
                .as_array_mut()
                .ok_or(ModelSchemaError::InvalidDocument)?;
            if variants.len() != projected.len() {
                return Err(ModelSchemaError::InvalidDocument);
            }
            for (variant, value) in variants.iter().zip(projected.iter_mut()) {
                simplify(
                    object(variant).ok_or(ModelSchemaError::InvalidDocument)?,
                    value,
                    remaining,
                )?;
            }
        }
    }
    if let Some(items) = field(source, "items").and_then(object) {
        simplify(items, &mut target["items"], remaining)?;
    }
    Ok(())
}

fn nullable_default<'a, 'src>(
    parent: &'a Fields<'src>,
    name: &str,
    field_schema: &'a Fields<'src>,
    projected: &Value,
) -> Option<(usize, &'a Fields<'src>)> {
    let required = field(parent, "required")
        .and_then(array)
        .unwrap_or_default();
    if required.iter().any(|item| text(Some(item)) == Some(name))
        || !matches!(field(field_schema, "default"), Some(JsonValue::Null))
        || !projected
            .as_object()
            .is_some_and(|fields| fields.len() == 1 && fields.contains_key("anyOf"))
    {
        return None;
    }
    let [first, second] = field(field_schema, "anyOf").and_then(array)? else {
        return None;
    };
    let null = |node| {
        object(node)
            .is_some_and(|fields| fields.len() == 1 && text(field(fields, "type")) == Some("null"))
    };
    match (null(first), null(second)) {
        (true, false) => Some((1, object(second)?)),
        (false, true) => Some((0, object(first)?)),
        _ => None,
    }
}
