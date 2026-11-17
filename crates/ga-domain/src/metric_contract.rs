mod unicode_decimal;

use std::{collections::BTreeSet, error::Error, fmt};

use serde_json::{Map, Value, json};

use crate::{
    event_contract::metric_definition_hash,
    event_payload::{finite_number, numeric_text, python_whitespace},
};

#[derive(Debug)]
pub enum MetricContractError {
    Shape,
    Key,
    Labels,
    Aggregation,
    Units,
    Bounds,
    Scale,
    Coverage,
    Category,
}

impl fmt::Display for MetricContractError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Shape => "Metric definition has invalid fields or time semantics",
            Self::Key => "User metric key is invalid",
            Self::Labels => "Metric labels are empty, invalid or too long",
            Self::Aggregation => "Metric aggregation is incompatible with its values",
            Self::Units => "Metric unit and dimension do not match",
            Self::Bounds => "Numeric metric bounds are invalid",
            Self::Scale => "Metric scale information is invalid",
            Self::Coverage => "Metric coverage information is invalid",
            Self::Category => "Metric category domain is invalid",
        })
    }
}
impl Error for MetricContractError {}

pub struct UserMetricContract {
    key: String,
    contract: Value,
    hash: String,
}

impl UserMetricContract {
    /// Normalize a user-managed metric exactly as the existing storage contract.
    pub fn from_json(document: &Value) -> Result<Self, MetricContractError> {
        let object = document.as_object().ok_or(MetricContractError::Shape)?;
        if object.keys().any(|key| {
            !matches!(
                key.as_str(),
                "key"
                    | "labels"
                    | "value_kind"
                    | "unit"
                    | "dimension"
                    | "scale_id"
                    | "scale_version"
                    | "aggregation"
                    | "allowed_methods"
                    | "category_domain"
                    | "coverage"
                    | "time_semantics"
                    | "minimum"
                    | "maximum"
            )
        }) {
            return Err(MetricContractError::Shape);
        }
        let key = document["key"]
            .as_str()
            .filter(|key| {
                key.strip_prefix("user.")
                    .is_some_and(|suffix| stable_id(suffix, 127))
            })
            .ok_or(MetricContractError::Key)?;
        let labels = document["labels"]
            .as_object()
            .filter(|labels| (1..=8).contains(&labels.len()) && labels.values().all(bounded_label))
            .ok_or(MetricContractError::Labels)?;
        let value_kind = document["value_kind"]
            .as_str()
            .ok_or(MetricContractError::Shape)?;
        let supported = methods_for(value_kind).ok_or(MetricContractError::Shape)?;
        let methods: BTreeSet<_> = document["allowed_methods"]
            .as_array()
            .ok_or(MetricContractError::Aggregation)?
            .iter()
            .map(|method| {
                method
                    .as_str()
                    .filter(|method| supported.contains(method))
                    .ok_or(MetricContractError::Aggregation)
            })
            .collect::<Result<_, _>>()?;
        let aggregation = document["aggregation"]
            .as_str()
            .filter(|method| methods.contains(method))
            .ok_or(MetricContractError::Aggregation)?;
        let time = document["time_semantics"]
            .as_str()
            .filter(|value| matches!(*value, "point" | "interval"))
            .ok_or(MetricContractError::Shape)?;
        let dimension = document["dimension"]
            .as_str()
            .filter(|name| dimension_name(name))
            .ok_or(MetricContractError::Units)?;
        let unit = string_or_null(&document["unit"], MetricContractError::Units)?;
        let scale = string_or_null(&document["scale_id"], MetricContractError::Scale)?;
        let scale_version = integer_or_null(
            &document["scale_version"],
            i64::MAX,
            MetricContractError::Scale,
        )?;
        if value_kind == "ordinal" {
            if !scale.is_some_and(|name| stable_id(name, 128)) || scale_version.is_none() {
                return Err(MetricContractError::Scale);
            }
        } else if scale.is_some() || scale_version.is_some() {
            return Err(MetricContractError::Scale);
        }
        let minimum = number_or_null(
            &document["minimum"],
            -f64::MAX,
            f64::MAX,
            MetricContractError::Bounds,
        )?;
        let maximum = number_or_null(
            &document["maximum"],
            -f64::MAX,
            f64::MAX,
            MetricContractError::Bounds,
        )?;
        if matches!(value_kind, "nominal" | "boolean") {
            if unit.is_some() || minimum.is_some() || maximum.is_some() {
                return Err(MetricContractError::Units);
            }
        } else {
            if unit
                .filter(|unit| unit.chars().count() <= 32)
                .and_then(metric_unit_dimension)
                != Some(dimension)
            {
                return Err(MetricContractError::Units);
            }
            if !matches!((minimum, maximum), (Some(minimum), Some(maximum)) if minimum <= maximum) {
                return Err(MetricContractError::Bounds);
            }
        }
        let category = category_domain(value_kind, &document["category_domain"])?;
        let coverage = coverage_policy(&document["coverage"])?;
        let contract = json!({
            "key": key, "labels": labels, "value_kind": value_kind,
            "unit": unit, "dimension": dimension, "scale_id": scale, "scale_version": scale_version,
            "aggregation": aggregation, "allowed_methods": methods, "category_domain": category,
            "coverage": coverage, "time_semantics": time, "minimum": minimum, "maximum": maximum,
        });
        let hash = metric_definition_hash(&contract).map_err(|_| MetricContractError::Shape)?;
        Ok(Self {
            key: key.to_owned(),
            contract,
            hash,
        })
    }

    pub fn key(&self) -> &str {
        &self.key
    }
    pub fn contract(&self) -> &Value {
        &self.contract
    }
    pub fn hash(&self) -> &str {
        &self.hash
    }
}

pub fn methods_for(kind: &str) -> Option<&'static [&'static str]> {
    match kind {
        "physical_number" => Some(&["latest", "mean", "min", "max", "distribution"]),
        "increment" | "interval_total" => Some(&["sum"]),
        "cumulative_counter" => Some(&["delta", "latest"]),
        "ordinal" => Some(&["latest", "median", "distribution"]),
        "nominal" => Some(&["latest", "counts", "mode"]),
        "boolean" => Some(&["latest", "count_true", "rate"]),
        _ => None,
    }
}

pub fn metric_unit_dimension(unit: &str) -> Option<&'static str> {
    match unit {
        "1" => Some("dimensionless"),
        "%" => Some("ratio"),
        "bpm" | "rpm" => Some("frequency"),
        "count" | "steps" => Some("count"),
        "ms" | "s" | "minutes" | "hours" => Some("duration"),
        "m" | "km" => Some("distance"),
        "ml" | "L" => Some("volume"),
        "mg" | "g" | "kg" => Some("mass"),
        "m/s" | "km/h" => Some("speed"),
        "s/km" => Some("pace"),
        "score" => Some("ordinal"),
        unit => {
            let bounds = unit.strip_prefix("score_")?;
            let unsigned_lower = bounds.strip_prefix('-').unwrap_or(bounds);
            let (lower, upper) = unsigned_lower.split_once('-')?;
            let upper = upper.strip_prefix('-').unwrap_or(upper);
            (digits(lower) && digits(upper)).then_some("ordinal")
        }
    }
}

fn digits(value: &str) -> bool {
    !value.is_empty()
        && value
            .chars()
            .all(|character| decimal_value(character).is_some())
}

pub(crate) fn decimal_value(character: char) -> Option<u8> {
    let codepoint = u32::from(character);
    let ranges = unicode_decimal::DECIMAL_RANGES;
    let index = ranges.partition_point(|&(_, last)| last < codepoint);
    let &(first, _) = ranges
        .get(index)
        .filter(|&&(first, _)| first <= codepoint)?;
    // Unicode decimal digits occur in consecutive runs of ten. Some adjacent
    // alphabets are grouped into one range in the existing table.
    Some(((codepoint - first) % 10) as u8)
}

fn bounded_text(value: &Value, length: usize) -> Option<&str> {
    value.as_str().filter(|text| {
        text.chars().count() <= length && !text.trim_matches(python_whitespace).is_empty()
    })
}

fn bounded_label(label: &Value) -> bool {
    bounded_text(label, 120).is_some()
}

fn stable_id(value: &str, length: usize) -> bool {
    let mut bytes = value.bytes();
    value.len() <= length
        && bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'.' | b'-')
        })
}

fn dimension_name(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 32
        && bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        })
}

fn string_or_null(
    value: &Value,
    error: MetricContractError,
) -> Result<Option<&str>, MetricContractError> {
    if value.is_null() {
        Ok(None)
    } else {
        value.as_str().map(Some).ok_or(error)
    }
}

fn number_or_null(
    value: &Value,
    low: f64,
    high: f64,
    error: MetricContractError,
) -> Result<Option<f64>, MetricContractError> {
    if value.is_null() {
        return Ok(None);
    }
    finite_number(low, high, true)(value.clone())
        .and_then(|value| value.as_f64())
        .map(Some)
        .ok_or(error)
}

fn integer_or_null(
    value: &Value,
    maximum: i64,
    error: MetricContractError,
) -> Result<Option<i64>, MetricContractError> {
    if value.is_null() {
        return Ok(None);
    }
    let parsed = match value {
        Value::Bool(value) => Some(i64::from(*value)),
        Value::String(text) => parse_int_text(text),
        Value::Number(value) => value.as_i64().or_else(|| {
            let number = value.as_f64()?;
            (number.fract() == 0.0 && number < 9_223_372_036_854_775_808.0).then_some(number as i64)
        }),
        _ => None,
    };
    parsed
        .filter(|number| (1..=maximum).contains(number))
        .map(Some)
        .ok_or(error)
}

fn parse_int_text(text: &str) -> Option<i64> {
    let text = text.trim_matches(python_whitespace);
    let integral = match text.split_once('.') {
        Some((integral, fraction))
            if !fraction.is_empty() && fraction.bytes().all(|byte| byte == b'0') =>
        {
            integral
        }
        Some(_) => return None,
        None => text,
    };
    numeric_text(integral)?.parse().ok()
}

fn category_domain<'a>(
    kind: &str,
    value: &'a Value,
) -> Result<Option<Vec<&'a str>>, MetricContractError> {
    if value.is_null() {
        return Ok(None);
    }
    let categories = value
        .as_array()
        .filter(|categories| kind == "nominal" && (1..=50).contains(&categories.len()))
        .ok_or(MetricContractError::Category)?;
    let mut seen = BTreeSet::new();
    let mut domain = Vec::with_capacity(categories.len());
    for value in categories {
        let value = bounded_text(value, 200).ok_or(MetricContractError::Category)?;
        if !seen.insert(value) {
            return Err(MetricContractError::Category);
        }
        domain.push(value);
    }
    Ok(Some(domain))
}

fn coverage_policy(value: &Value) -> Result<Value, MetricContractError> {
    let policy: &Map<String, Value> = value.as_object().ok_or(MetricContractError::Coverage)?;
    if policy
        .keys()
        .any(|key| !matches!(key.as_str(), "kind" | "minimum_ratio" | "max_gap_seconds"))
    {
        return Err(MetricContractError::Coverage);
    }
    let kind = value["kind"]
        .as_str()
        .ok_or(MetricContractError::Coverage)?;
    let ratio = number_or_null(
        &value["minimum_ratio"],
        0.0,
        1.0,
        MetricContractError::Coverage,
    )?;
    let gap = integer_or_null(
        &value["max_gap_seconds"],
        86_400,
        MetricContractError::Coverage,
    )?;
    match (kind, ratio, gap) {
        ("time_weighted", Some(_), Some(_)) | ("all_values" | "sparse", None, None) => {
            Ok(json!({"kind": kind, "minimum_ratio": ratio, "max_gap_seconds": gap}))
        }
        _ => Err(MetricContractError::Coverage),
    }
}
