use std::collections::BTreeSet;

use ga_domain::{
    event_schema::CustomDefinition,
    metric_contract::{UserMetricContract, methods_for, metric_unit_dimension},
};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::CustomRegistryError;
use crate::system_metrics::register_user_metric_in;

pub(super) async fn bind_tracker_metrics(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    version: Uuid,
    definition: &CustomDefinition,
) -> Result<(), CustomRegistryError> {
    let contract = definition.contract();
    let schema = &contract["schema"];
    let fields = contract["fields"]
        .as_object()
        .ok_or(CustomRegistryError::Metric)?;
    for (name, field) in fields {
        if field["semantic"] == "text" {
            continue;
        }
        let id = field["id"].as_str().ok_or(CustomRegistryError::Metric)?;
        let metric = build_field_metric(
            txn,
            owner,
            field,
            &schema["properties"][name],
            &schema["$defs"],
        )
        .await?;
        insert_mapping(txn, owner, version, id, &metric).await?;
    }
    if contract["derived_duration"] == true {
        let locale = contract["labels"]
            .as_object()
            .and_then(|labels| labels.keys().min_by_key(|key| (key.len(), key.as_str())))
            .ok_or(CustomRegistryError::Metric)?;
        let label = if locale == "ru" {
            "Длительность (мин)"
        } else {
            "Duration (minutes)"
        };
        let metric = UserMetricContract::from_json(&json!({
            "key": format!("{}.elapsed_minutes", definition.key()),
            "labels": {locale: label}, "value_kind": "interval_total",
            "unit": "minutes", "dimension": "duration", "aggregation": "sum",
            "allowed_methods": ["sum"], "coverage": {"kind": "all_values"},
            "time_semantics": "interval", "minimum": 0, "maximum": 1e10,
        }))
        .map_err(|_| CustomRegistryError::Metric)?;
        insert_mapping(
            txn,
            owner,
            version,
            "__derived_duration_minutes_v1__",
            &metric,
        )
        .await?;
    }
    Ok(())
}

async fn insert_mapping(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    version: Uuid,
    field: &str,
    metric: &UserMetricContract,
) -> Result<(), CustomRegistryError> {
    let metric = register_user_metric_in(txn, owner, metric)
        .await
        .map_err(|_| CustomRegistryError::Metric)?;
    sqlx::query(
        "INSERT INTO event_metric_mappings
           (id, event_definition_version_id, field_id, metric_definition_version_id, projection_version)
         VALUES ($1,$2,$3,$4,1)",
    )
    .bind(Uuid::new_v4())
    .bind(version)
    .bind(field)
    .bind(metric.id)
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn build_field_metric(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    field: &Value,
    node: &Value,
    definitions: &Value,
) -> Result<UserMetricContract, CustomRegistryError> {
    let mut source = Vec::new();
    source_nodes(node, definitions, &mut source)?;
    source.retain(|node| node["type"] != "null");
    let semantic = field["semantic"]
        .as_str()
        .ok_or(CustomRegistryError::Metric)?;
    let key = field["id"].as_str().ok_or(CustomRegistryError::Metric)?;
    let mut contract = json!({
        "key": key, "labels": field["labels"], "coverage": {"kind": "all_values"},
        "time_semantics": "point",
    });
    let (kind, dimension, aggregation) = match semantic {
        "nominal" => {
            let categories = source
                .iter()
                .flat_map(|node| node["enum"].as_array().into_iter().flatten())
                .map(|value| match value {
                    Value::Null => Ok("None"),
                    Value::String(text) => Ok(text.as_str()),
                    _ => Err(CustomRegistryError::Metric),
                })
                .collect::<Result<BTreeSet<_>, _>>()?;
            if !categories.is_empty() {
                contract["category_domain"] = json!(categories);
            }
            validate_domain(node, definitions, semantic, None)?;
            ("nominal", "category", "counts")
        }
        "boolean" => {
            validate_domain(node, definitions, semantic, None)?;
            ("boolean", "boolean", "rate")
        }
        "ordinal" | "count" | "quantity" => {
            let (min, max) = source_bounds(&source)?;
            validate_domain(node, definitions, semantic, Some((min, max)))?;
            let unit = field["unit"].as_str().ok_or(CustomRegistryError::Metric)?;
            contract["unit"] = json!(unit);
            contract["minimum"] = json!(min);
            contract["maximum"] = json!(max);
            if semantic == "ordinal" {
                contract["scale_id"] = json!(key);
                contract["scale_version"] =
                    json!(scale_version(txn, owner, key, unit, min, max).await?);
                ("ordinal", "ordinal", "median")
            } else {
                let dimension = metric_unit_dimension(unit).ok_or(CustomRegistryError::Metric)?;
                match field["metric_semantics"].as_str() {
                    Some("interval_total") if min >= 0.0 => {
                        contract["time_semantics"] = json!("interval");
                        ("interval_total", dimension, "sum")
                    }
                    Some("cumulative_counter") if min >= 0.0 => {
                        ("cumulative_counter", dimension, "delta")
                    }
                    Some("event_count" | "event_total") if min >= 0.0 => {
                        ("increment", dimension, "sum")
                    }
                    Some("gauge") | None => ("physical_number", dimension, "mean"),
                    _ => return Err(CustomRegistryError::Metric),
                }
            }
        }
        _ => return Err(CustomRegistryError::Metric),
    };
    contract["value_kind"] = json!(kind);
    contract["dimension"] = json!(dimension);
    contract["aggregation"] = json!(aggregation);
    contract["allowed_methods"] = json!(methods_for(kind).ok_or(CustomRegistryError::Metric)?);
    UserMetricContract::from_json(&contract).map_err(|_| CustomRegistryError::Metric)
}

async fn scale_version(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    key: &str,
    unit: &str,
    min: f64,
    max: f64,
) -> Result<i32, CustomRegistryError> {
    type CurrentScale = (Option<i32>, Option<String>, Option<f64>, Option<f64>);
    let current = sqlx::query_as::<_, CurrentScale>(
        "SELECT v.scale_version,v.unit,v.minimum,v.maximum FROM metric_definitions d
         JOIN metric_definition_versions v ON v.definition_id=d.id AND v.version=d.current_version
         WHERE d.key=$1 AND d.owner_id=$2 AND d.namespace='user'",
    )
    .bind(key)
    .bind(owner)
    .fetch_optional(&mut **txn)
    .await?;
    if let Some((version, current_unit, lower, upper)) = current {
        if current_unit.as_deref() == Some(unit) && lower == Some(min) && upper == Some(max) {
            version.ok_or(CustomRegistryError::Metric)
        } else {
            version
                .unwrap_or_default()
                .checked_add(1)
                .ok_or(CustomRegistryError::Metric)
        }
    } else {
        Ok(1)
    }
}

fn reference<'a>(
    node: &Value,
    definitions: &'a Value,
) -> Result<Option<&'a Value>, CustomRegistryError> {
    node.get("$ref")
        .map(|reference| {
            let name = reference
                .as_str()
                .and_then(|reference| reference.strip_prefix("#/$defs/"))
                .ok_or(CustomRegistryError::Metric)?;
            definitions.get(name).ok_or(CustomRegistryError::Metric)
        })
        .transpose()
}

// The admitted schema profile bounds depth and the number of resolved branches.
fn source_nodes<'a>(
    node: &'a Value,
    definitions: &'a Value,
    result: &mut Vec<&'a Value>,
) -> Result<(), CustomRegistryError> {
    if let Some(target) = reference(node, definitions)? {
        return source_nodes(target, definitions, result);
    }
    let start = result.len();
    for keyword in ["oneOf", "anyOf"] {
        if let Some(choices) = node[keyword].as_array() {
            for choice in choices {
                source_nodes(choice, definitions, result)?;
            }
        }
    }
    if result.len() == start {
        result.push(node);
    }
    Ok(())
}

fn source_bounds(nodes: &[&Value]) -> Result<(f64, f64), CustomRegistryError> {
    let mut lower = f64::INFINITY;
    let mut upper = f64::NEG_INFINITY;
    for node in nodes {
        let min = node
            .get("minimum")
            .or_else(|| node.get("exclusiveMinimum"))
            .and_then(Value::as_f64)
            .ok_or(CustomRegistryError::Metric)?;
        let max = node
            .get("maximum")
            .or_else(|| node.get("exclusiveMaximum"))
            .and_then(Value::as_f64)
            .ok_or(CustomRegistryError::Metric)?;
        lower = lower.min(min);
        upper = upper.max(max);
    }
    if lower > upper {
        return Err(CustomRegistryError::Metric);
    }
    Ok((lower, upper))
}

fn resolve_nodes(
    node: &Value,
    definitions: &Value,
    result: &mut Vec<Map<String, Value>>,
    remaining: &mut usize,
) -> Result<(), CustomRegistryError> {
    *remaining = remaining
        .checked_sub(1)
        .ok_or(CustomRegistryError::Metric)?;
    let fields = node.as_object().ok_or(CustomRegistryError::Metric)?;
    if let Some(target) = reference(node, definitions)? {
        let start = result.len();
        resolve_nodes(target, definitions, result, remaining)?;
        for resolved in &mut result[start..] {
            intersect(resolved, fields, "$ref");
        }
        return Ok(());
    }
    let start = result.len();
    for keyword in ["oneOf", "anyOf"] {
        if let Some(choices) = node[keyword].as_array() {
            for choice in choices {
                let mut branch = choice
                    .as_object()
                    .cloned()
                    .ok_or(CustomRegistryError::Metric)?;
                intersect(&mut branch, fields, keyword);
                resolve_nodes(&Value::Object(branch), definitions, result, remaining)?;
            }
        }
    }
    if start == result.len() {
        result.push(fields.clone());
    }
    Ok(())
}

fn intersect(base: &mut Map<String, Value>, siblings: &Map<String, Value>, excluded: &str) {
    for (key, value) in siblings {
        if key == excluded {
            continue;
        }
        let combined = match (key.as_str(), base.get(key)) {
            ("enum", Some(Value::Array(items))) => value.as_array().map(|other| {
                Value::Array(
                    items
                        .iter()
                        .filter(|item| other.iter().any(|candidate| same_literal(item, candidate)))
                        .cloned()
                        .collect(),
                )
            }),
            (
                bound @ ("minimum" | "exclusiveMinimum" | "maximum" | "exclusiveMaximum"),
                Some(previous),
            ) => match (previous.as_f64(), value.as_f64()) {
                (Some(left), Some(right))
                    if matches!(bound, "minimum" | "exclusiveMinimum") && left > right =>
                {
                    Some(previous.clone())
                }
                (Some(left), Some(right))
                    if matches!(bound, "maximum" | "exclusiveMaximum") && left < right =>
                {
                    Some(previous.clone())
                }
                _ => None,
            },
            _ => None,
        };
        base.insert(key.clone(), combined.unwrap_or_else(|| value.clone()));
    }
}

fn same_literal(first: &Value, second: &Value) -> bool {
    let numeric = |value: &Value| {
        value
            .as_f64()
            .or_else(|| value.as_bool().map(|truth| f64::from(u8::from(truth))))
    };
    first == second || matches!((numeric(first), numeric(second)), (Some(a), Some(b)) if a == b)
}

fn literals(node: &Map<String, Value>) -> Option<&[Value]> {
    node.get("enum")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .or_else(|| node.get("const").map(std::slice::from_ref))
}

fn scalar_type(value: &Value) -> &str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(number) if number.is_i64() || number.is_u64() => "integer",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        _ => "unsupported",
    }
}

fn validate_domain(
    node: &Value,
    definitions: &Value,
    semantic: &str,
    bounds: Option<(f64, f64)>,
) -> Result<(), CustomRegistryError> {
    let mut nodes = Vec::new();
    // Combined alternatives can expand more branches than the standalone schema walker.
    const MAX_BINDING_NODES: usize = 4_096;
    let mut remaining = MAX_BINDING_NODES;
    resolve_nodes(node, definitions, &mut nodes, &mut remaining)?;
    let valid_types: &[&str] = match semantic {
        "nominal" => &["string"],
        "boolean" => &["boolean"],
        _ => &["integer", "number"],
    };
    let mut found_type = false;
    let mut found_bounds = false;
    for fields in &nodes {
        let field_type = fields.get("type").and_then(Value::as_str);
        let literal = literals(fields);
        let types = field_type
            .into_iter()
            .chain(
                literal
                    .into_iter()
                    .flatten()
                    .filter(|_| field_type.is_none())
                    .map(scalar_type),
            )
            .filter(|kind| *kind != "null");
        for kind in types {
            if !valid_types.contains(&kind) {
                return Err(CustomRegistryError::Metric);
            }
            found_type = true;
        }
        if field_type == Some("null") {
            continue;
        }
        if let Some((min, max)) = bounds {
            let bound =
                |key: &str, fallback| fields.get(key).and_then(Value::as_f64).unwrap_or(fallback);
            let values = literal.into_iter().flatten().filter_map(Value::as_f64);
            let low_literal = values.clone().reduce(f64::min);
            let high_literal = values.reduce(f64::max);
            let lower = bound("minimum", f64::NEG_INFINITY)
                .max(bound("exclusiveMinimum", f64::NEG_INFINITY))
                .max(low_literal.unwrap_or(f64::NEG_INFINITY));
            let upper = bound("maximum", f64::INFINITY)
                .min(bound("exclusiveMaximum", f64::INFINITY))
                .min(high_literal.unwrap_or(f64::INFINITY));
            if !lower.is_finite() || !upper.is_finite() || lower < min || upper > max {
                return Err(CustomRegistryError::Metric);
            }
            found_bounds |= literal.is_none() || low_literal.is_some();
        }
        if semantic == "nominal" {
            let fits = match literal {
                Some(values) => values
                    .iter()
                    .filter_map(Value::as_str)
                    .all(|value| value.chars().count() <= 500),
                None => fields
                    .get("maxLength")
                    .and_then(Value::as_u64)
                    .is_some_and(|size| size <= 500),
            };
            if !fits {
                return Err(CustomRegistryError::Metric);
            }
        }
    }
    if !found_type
        || (bounds.is_some() && !found_bounds)
        || (semantic == "nominal" && !bounded_nominal(node, definitions)?)
    {
        return Err(CustomRegistryError::Metric);
    }
    Ok(())
}

fn bounded_nominal(node: &Value, definitions: &Value) -> Result<bool, CustomRegistryError> {
    if node["type"] == "null" {
        return Ok(true);
    }
    if let Some(target) = reference(node, definitions)? {
        return bounded_nominal(target, definitions);
    }
    if let Some(choices) = node
        .get("oneOf")
        .or_else(|| node.get("anyOf"))
        .and_then(Value::as_array)
    {
        for choice in choices {
            if !bounded_nominal(choice, definitions)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    let fields = node.as_object().ok_or(CustomRegistryError::Metric)?;
    Ok(match literals(fields) {
        Some(values) => values.iter().all(|value| {
            value.is_null()
                || value
                    .as_str()
                    .is_some_and(|value| (1..=500).contains(&value.chars().count()))
        }),
        None => {
            node["minLength"].as_u64().is_some_and(|size| size >= 1)
                && node["maxLength"].as_u64().is_some_and(|size| size <= 500)
        }
    })
}
