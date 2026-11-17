use std::{collections::BTreeSet, error::Error, fmt};

use serde_json::{Map, Value, json};

use crate::event_contract::python_ascii_json;

use super::check_value_limits;

type Node = Map<String, Value>;

const DIALECT: &str = "https://json-schema.org/draft/2020-12/schema";
const MAX_SCHEMA_BYTES: usize = 32_768;
const MAX_EXPANSIONS: usize = 64;
const MAX_DEPTH: u8 = 8;
const REFERENCE_PREFIX: &str = "#/$defs/";
const KEYWORDS: &[&str] = &[
    "$schema",
    "$defs",
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "oneOf",
    "anyOf",
    "title",
    "description",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CustomSchemaError {
    Shape,
    Size,
    Keyword,
    Constraint,
    Reference,
    Contradiction,
}

impl fmt::Display for CustomSchemaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Shape => "Custom schema must use the supported closed object shape",
            Self::Size => "Custom schema exceeds its size or nesting limit",
            Self::Keyword => "Custom schema contains an unsupported keyword or dialect",
            Self::Constraint => "Custom schema contains an invalid value constraint",
            Self::Reference => "Custom schema has an invalid or excessively complex reference",
            Self::Contradiction => "Custom schema has contradictory value constraints",
        })
    }
}
impl Error for CustomSchemaError {}

/// Check the bounded Draft 2020-12 profile used for user-defined diary entries.
pub fn validate_custom_schema(schema: &Value) -> Result<(), CustomSchemaError> {
    let root = schema.as_object().ok_or(CustomSchemaError::Shape)?;
    if root.get("type").and_then(Value::as_str) != Some("object")
        || root.get("additionalProperties") != Some(&Value::Bool(false))
    {
        return Err(CustomSchemaError::Shape);
    }
    if root
        .get("$schema")
        .is_some_and(|dialect| !dialect.is_null() && dialect != DIALECT)
    {
        return Err(CustomSchemaError::Keyword);
    }
    let mut nodes = Vec::new();
    walk(root, 0, &mut nodes)?;
    let text = python_ascii_json(schema).map_err(|_| CustomSchemaError::Size)?;
    if text.len() > MAX_SCHEMA_BYTES {
        return Err(CustomSchemaError::Size);
    }

    let definitions = map(root.get("$defs"))?;
    let mut remaining = MAX_EXPANSIONS - 1;
    let mut path = Vec::new();
    for node in &nodes {
        if let Some(target) = reference(node)? {
            expand(target, definitions, &mut remaining, &mut path)?;
        }
    }
    if !jsonschema::draft202012::meta::is_valid(schema) {
        return Err(CustomSchemaError::Constraint);
    }
    let empty = Map::new();
    for node in nodes {
        if literals(node).next().is_none() {
            continue;
        }
        let mut constraints = node.clone();
        constraints.remove("const");
        constraints.remove("enum");
        if constraints.is_empty() {
            continue;
        }
        let test = json!({"$defs": definitions.unwrap_or(&empty), "allOf": [constraints]});
        let validator = jsonschema::draft202012::options()
            .should_validate_formats(false)
            .build(&test)
            .map_err(|_| CustomSchemaError::Constraint)?;
        if literals(node).any(|literal| !validator.is_valid(literal)) {
            return Err(CustomSchemaError::Contradiction);
        }
    }
    Ok(())
}

fn walk<'a>(node: &'a Node, depth: u8, nodes: &mut Vec<&'a Node>) -> Result<(), CustomSchemaError> {
    if depth > MAX_DEPTH {
        return Err(CustomSchemaError::Size);
    }
    nodes.push(node);
    if node.keys().any(|key| !KEYWORDS.contains(&key.as_str()))
        || (depth > 0 && node.contains_key("$schema"))
    {
        return Err(CustomSchemaError::Keyword);
    }
    reference(node)?;
    let kind = node
        .get("type")
        .map(|kind| kind.as_str().ok_or(CustomSchemaError::Shape))
        .transpose()?;
    if kind.is_some_and(|kind| {
        !matches!(
            kind,
            "object" | "array" | "string" | "integer" | "number" | "boolean" | "null"
        )
    }) || (kind != Some("object")
        && has(node, &["properties", "required", "additionalProperties"]))
        || (kind != Some("array") && has(node, &["items", "minItems", "maxItems"]))
        || !has(node, &["type", "$ref", "const", "enum", "oneOf", "anyOf"])
    {
        return Err(CustomSchemaError::Shape);
    }
    match kind {
        Some("object") if node.get("additionalProperties") != Some(&Value::Bool(false)) => {
            return Err(CustomSchemaError::Shape);
        }
        Some("array") => {
            if !node.contains_key("items") {
                return Err(CustomSchemaError::Constraint);
            }
            bounded(node, "minItems", "maxItems", 1_000)?;
        }
        Some("string") if !has(node, &["enum", "const"]) => {
            bounded(node, "minLength", "maxLength", 16_000)?;
        }
        Some("integer" | "number") => finite_numbers(node, kind == Some("integer"))?,
        _ => (),
    }
    if ["title", "description"].iter().any(|keyword| {
        node.get(*keyword)
            .is_some_and(|text| text.as_str().is_none_or(|text| text.chars().count() > 500))
    }) {
        return Err(CustomSchemaError::Constraint);
    }
    let properties = map(node.get("properties"))?;
    if let Some(properties) = properties {
        walk_children(properties, 32, field_name, depth, nodes)?;
    }
    if let Some(definitions) = map(node.get("$defs"))? {
        walk_children(definitions, 16, definition_name, depth, nodes)?;
    }
    if let Some(item) = node.get("items") {
        walk(
            item.as_object().ok_or(CustomSchemaError::Shape)?,
            depth + 1,
            nodes,
        )?;
    }
    for keyword in ["oneOf", "anyOf"] {
        if let Some(choices) = node.get(keyword) {
            let choices = choices
                .as_array()
                .filter(|choices| (1..=4).contains(&choices.len()))
                .ok_or(CustomSchemaError::Constraint)?;
            let mut earlier = Vec::new();
            for choice in choices {
                let choice = choice.as_object().ok_or(CustomSchemaError::Shape)?;
                walk(choice, depth + 1, nodes)?;
                if keyword == "oneOf" {
                    let mut comparable = choice.clone();
                    comparable.remove("title");
                    comparable.remove("description");
                    if let Some(literal) = comparable.remove("const") {
                        comparable.insert("enum".to_owned(), json!([literal]));
                    }
                    let comparable = Value::Object(comparable);
                    if earlier.iter().any(|prior| python_equal(prior, &comparable)) {
                        return Err(CustomSchemaError::Contradiction);
                    }
                    earlier.push(comparable);
                }
            }
        }
    }
    if let Some(required) = node.get("required") {
        let required = required.as_array().ok_or(CustomSchemaError::Constraint)?;
        let mut seen = BTreeSet::new();
        for field in required {
            let field = field.as_str().ok_or(CustomSchemaError::Constraint)?;
            if properties.is_none_or(|properties| !properties.contains_key(field))
                || !seen.insert(field)
            {
                return Err(CustomSchemaError::Constraint);
            }
        }
    }
    if let Some(members) = node.get("enum") {
        let members = members
            .as_array()
            .filter(|members| (1..=50).contains(&members.len()))
            .ok_or(CustomSchemaError::Constraint)?;
        if members
            .iter()
            .any(|member| member.is_object() || member.is_array())
        {
            return Err(CustomSchemaError::Constraint);
        }
    }
    for literal in literals(node) {
        check_value_limits(literal, depth).map_err(|_| CustomSchemaError::Size)?;
    }
    Ok(())
}

fn has(node: &Node, keys: &[&str]) -> bool {
    keys.iter().any(|key| node.contains_key(*key))
}

fn map(node: Option<&Value>) -> Result<Option<&Node>, CustomSchemaError> {
    node.map(|node| node.as_object().ok_or(CustomSchemaError::Shape))
        .transpose()
}

fn literals(node: &Node) -> impl Iterator<Item = &Value> {
    node.get("const").into_iter().chain(
        node.get("enum")
            .and_then(Value::as_array)
            .into_iter()
            .flatten(),
    )
}

fn field_name(name: &str) -> bool {
    let mut chars = name.bytes();
    name.len() <= 63
        && chars.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && chars.all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
}

fn definition_name(name: &str) -> bool {
    let mut chars = name.bytes();
    name.len() <= 63
        && chars.next().is_some_and(|byte| byte.is_ascii_alphabetic())
        && chars.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn walk_children<'a>(
    children: &'a Node,
    max: usize,
    eligible: impl Fn(&str) -> bool,
    depth: u8,
    nodes: &mut Vec<&'a Node>,
) -> Result<(), CustomSchemaError> {
    if children.len() > max {
        return Err(CustomSchemaError::Size);
    }
    for (key, child) in children {
        if !eligible(key) {
            return Err(CustomSchemaError::Shape);
        }
        walk(
            child.as_object().ok_or(CustomSchemaError::Shape)?,
            depth + 1,
            nodes,
        )?;
    }
    Ok(())
}

fn reference(node: &Node) -> Result<Option<&str>, CustomSchemaError> {
    node.get("$ref")
        .map(|value| {
            value
                .as_str()
                .and_then(|uri| uri.strip_prefix(REFERENCE_PREFIX))
                .filter(|name| definition_name(name))
                .ok_or(CustomSchemaError::Reference)
        })
        .transpose()
}

fn expand<'a>(
    target: &'a str,
    definitions: Option<&'a Node>,
    remaining: &mut usize,
    path: &mut Vec<&'a str>,
) -> Result<(), CustomSchemaError> {
    if path.len() >= usize::from(MAX_DEPTH) || path.contains(&target) || *remaining == 0 {
        return Err(CustomSchemaError::Reference);
    }
    *remaining -= 1;
    let definition = definitions
        .and_then(|definitions| definitions.get(target))
        .and_then(Value::as_object)
        .ok_or(CustomSchemaError::Reference)?;
    let mut subtree = Vec::new();
    walk(definition, 1, &mut subtree)?;
    path.push(target);
    for node in subtree {
        if let Some(next) = reference(node)? {
            expand(next, definitions, remaining, path)?;
        }
    }
    path.pop();
    Ok(())
}

fn bounded(node: &Node, minimum: &str, maximum: &str, limit: u64) -> Result<(), CustomSchemaError> {
    let minimum = node
        .get(minimum)
        .map_or(Some(0), Value::as_u64)
        .ok_or(CustomSchemaError::Constraint)?;
    let maximum = node
        .get(maximum)
        .and_then(Value::as_u64)
        .ok_or(CustomSchemaError::Constraint)?;
    if minimum <= maximum && maximum <= limit {
        Ok(())
    } else {
        Err(CustomSchemaError::Constraint)
    }
}

fn finite_numbers(node: &Node, integer_only: bool) -> Result<(), CustomSchemaError> {
    let number = |key| {
        node.get(key)
            .map(|bound| {
                bound
                    .as_f64()
                    .filter(|bound| bound.is_finite())
                    .ok_or(CustomSchemaError::Constraint)
            })
            .transpose()
    };
    let min = number("minimum")?.unwrap_or(f64::NEG_INFINITY);
    let max = number("maximum")?.unwrap_or(f64::INFINITY);
    let exclusive_min = number("exclusiveMinimum")?.unwrap_or(f64::NEG_INFINITY);
    let exclusive_max = number("exclusiveMaximum")?.unwrap_or(f64::INFINITY);
    let mut lower = min.max(exclusive_min);
    let mut upper = max.min(exclusive_max);
    let members = node.get("enum").and_then(Value::as_array);
    let singleton = node.get("const");
    let declared = members.map_or_else(
        || singleton.into_iter().collect::<Vec<_>>(),
        |values| values.iter().collect(),
    );
    let mut literal_min = f64::INFINITY;
    let mut literal_max = f64::NEG_INFINITY;
    for value in declared.into_iter().filter_map(Value::as_f64) {
        if value < min || value > max || value <= exclusive_min || value >= exclusive_max {
            return Err(CustomSchemaError::Contradiction);
        }
        literal_min = literal_min.min(value);
        literal_max = literal_max.max(value);
    }
    if literal_min.is_finite() {
        lower = lower.max(literal_min);
        upper = upper.min(literal_max);
    }
    if !lower.is_finite()
        || !upper.is_finite()
        || lower > upper
        || (lower == upper && (exclusive_min == lower || exclusive_max == upper))
    {
        return Err(CustomSchemaError::Constraint);
    }
    if integer_only {
        let first = if exclusive_min == lower {
            lower.floor() + 1.0
        } else {
            lower.ceil()
        };
        let last = if exclusive_max == upper {
            upper.ceil() - 1.0
        } else {
            upper.floor()
        };
        if first > last {
            return Err(CustomSchemaError::Contradiction);
        }
    }
    Ok(())
}

fn python_equal(left: &Value, right: &Value) -> bool {
    let number = |value: &Value| {
        value
            .as_f64()
            .or_else(|| value.as_bool().map(|value| f64::from(u8::from(value))))
    };
    if let (Some(left), Some(right)) = (number(left), number(right)) {
        return left == right;
    }
    match (left, right) {
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| python_equal(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, value)| {
                    right
                        .get(key)
                        .is_some_and(|other| python_equal(value, other))
                })
        }
        _ => left == right,
    }
}
