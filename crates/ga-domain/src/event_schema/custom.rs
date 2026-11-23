use std::{cmp::Ordering, collections::BTreeSet, error::Error, fmt};

use num_bigint::{BigInt, ToBigInt};

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
        let literal_schema =
            json!({"$defs": definitions.unwrap_or(&empty), "allOf": [constraints]});
        let validator = jsonschema::draft202012::options()
            .should_validate_formats(false)
            .build(&literal_schema)
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

pub(super) fn field_name(name: &str) -> bool {
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

pub(super) struct ExactNumber {
    // Python compares integer inputs without converting them to double.
    // Decimal/exponent inputs have already taken Python's binary64 path.
    integer: BigInt,
    fraction: f64,
    fits_f64: bool,
}

impl ExactNumber {
    pub(super) fn read(value: &Value) -> Option<Self> {
        let value = value.as_number()?;
        if let Ok(integer) = value.as_str().parse::<BigInt>() {
            return Some(Self {
                integer,
                fraction: 0.0,
                fits_f64: value.as_f64().is_some_and(f64::is_finite),
            });
        }
        let float = value.as_f64().filter(|value| value.is_finite())?;
        Some(Self {
            integer: float.to_bigint()?,
            fraction: float.fract(),
            fits_f64: true,
        })
    }

    pub(super) fn floor(&self) -> BigInt {
        &self.integer - i32::from(self.fraction < 0.0)
    }

    pub(super) fn ceil(&self) -> BigInt {
        &self.integer + i32::from(self.fraction > 0.0)
    }
}

impl PartialEq for ExactNumber {
    fn eq(&self, other: &Self) -> bool {
        self.integer == other.integer && self.fraction == other.fraction
    }
}
impl Eq for ExactNumber {}
impl PartialOrd for ExactNumber {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for ExactNumber {
    fn cmp(&self, other: &Self) -> Ordering {
        self.integer.cmp(&other.integer).then_with(|| {
            self.fraction
                .partial_cmp(&other.fraction)
                .unwrap_or(Ordering::Equal)
        })
    }
}

fn finite_numbers(node: &Node, integer_only: bool) -> Result<(), CustomSchemaError> {
    let number = |key| {
        node.get(key)
            .map(|bound| {
                ExactNumber::read(bound)
                    .filter(|bound| bound.fits_f64)
                    .ok_or(CustomSchemaError::Constraint)
            })
            .transpose()
    };
    let min = number("minimum")?;
    let max = number("maximum")?;
    let exclusive_min = number("exclusiveMinimum")?;
    let exclusive_max = number("exclusiveMaximum")?;
    let members = node.get("enum").and_then(Value::as_array);
    let singleton = node.get("const");
    let declared = members.map_or_else(
        || singleton.into_iter().collect::<Vec<_>>(),
        |values| values.iter().collect(),
    );
    let literals: Vec<_> = declared.into_iter().filter_map(ExactNumber::read).collect();
    for value in &literals {
        if min.as_ref().is_some_and(|bound| value < bound)
            || max.as_ref().is_some_and(|bound| value > bound)
            || exclusive_min.as_ref().is_some_and(|bound| value <= bound)
            || exclusive_max.as_ref().is_some_and(|bound| value >= bound)
        {
            return Err(CustomSchemaError::Contradiction);
        }
    }
    let lower = [min.as_ref(), exclusive_min.as_ref(), literals.iter().min()]
        .into_iter()
        .flatten()
        .max()
        .filter(|bound| bound.fits_f64)
        .ok_or(CustomSchemaError::Constraint)?;
    let upper = [max.as_ref(), exclusive_max.as_ref(), literals.iter().max()]
        .into_iter()
        .flatten()
        .min()
        .filter(|bound| bound.fits_f64)
        .ok_or(CustomSchemaError::Constraint)?;
    let open_lower = exclusive_min.as_ref() == Some(lower);
    let open_upper = exclusive_max.as_ref() == Some(upper);
    if lower > upper || (lower == upper && (open_lower || open_upper)) {
        return Err(CustomSchemaError::Constraint);
    }
    if integer_only {
        let first = if open_lower {
            lower.floor() + 1
        } else {
            lower.ceil()
        };
        let last = if open_upper {
            upper.ceil() - 1
        } else {
            upper.floor()
        };
        if first > last {
            return Err(CustomSchemaError::Contradiction);
        }
    }
    Ok(())
}

pub(crate) fn python_equal(left: &Value, right: &Value) -> bool {
    let number = |value: &Value| {
        ExactNumber::read(value).or_else(|| {
            value.as_bool().map(|value| ExactNumber {
                integer: BigInt::from(u8::from(value)),
                fraction: 0.0,
                fits_f64: true,
            })
        })
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
