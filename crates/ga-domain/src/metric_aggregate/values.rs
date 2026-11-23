use std::{cmp::Ordering, collections::HashMap};

use serde_json::{Map, Value};

use crate::legacy_json::{self, Utf8Layout, multiply_numbers, sum_numbers};

use super::{MetricAggregateError, MetricSample};

pub(super) fn number(value: &Value) -> Result<f64, MetricAggregateError> {
    match value {
        Value::Bool(value) => Ok(f64::from(u8::from(*value))),
        value => value
            .as_f64()
            .filter(|value| value.is_finite())
            .ok_or(MetricAggregateError::Type),
    }
}

pub(super) fn finite(value: f64) -> Result<Value, MetricAggregateError> {
    serde_json::Number::from_f64(value)
        .map(Value::Number)
        .ok_or(MetricAggregateError::Nonfinite)
}

pub(super) fn sum<'a>(
    values: impl IntoIterator<Item = &'a Value>,
) -> Result<Value, MetricAggregateError> {
    sum_numbers(values).map_err(|_| MetricAggregateError::Nonfinite)
}

fn compare(first: &Value, second: &Value) -> Result<Ordering, MetricAggregateError> {
    match (first, second) {
        (Value::String(first), Value::String(second)) => Ok(first.cmp(second)),
        _ => number(first)?
            .partial_cmp(&number(second)?)
            .ok_or(MetricAggregateError::Type),
    }
}

fn python_string(value: &Value) -> Result<String, MetricAggregateError> {
    match value {
        Value::Null => Ok("None".to_owned()),
        Value::Bool(true) => Ok("True".to_owned()),
        Value::Bool(false) => Ok("False".to_owned()),
        Value::String(value) => Ok(value.clone()),
        Value::Number(_) => {
            let bytes = serde_json::to_vec(value).map_err(|_| MetricAggregateError::Type)?;
            let encoded = legacy_json::encode(&bytes, Utf8Layout::Ordered, bytes.len())
                .map_err(|_| MetricAggregateError::Type)?;
            String::from_utf8(encoded).map_err(|_| MetricAggregateError::Type)
        }
        _ => Err(MetricAggregateError::Type),
    }
}

fn counts(samples: &[MetricSample]) -> Result<Value, MetricAggregateError> {
    let mut counts = Map::new();
    for sample in samples {
        let key = python_string(&sample.value)?;
        let count = counts.get(&key).and_then(Value::as_u64).unwrap_or(0) + 1;
        counts.insert(key, Value::from(count));
    }
    Ok(Value::Object(counts))
}

#[derive(Eq, PartialEq, Hash)]
enum CounterKey {
    Null,
    Number(u64),
    Text(String),
}

fn mode(samples: &[MetricSample]) -> Result<Value, MetricAggregateError> {
    let mut counts: HashMap<CounterKey, (usize, usize)> = HashMap::new();
    for (index, sample) in samples.iter().enumerate() {
        let key = match &sample.value {
            Value::Null => CounterKey::Null,
            Value::String(value) => CounterKey::Text(value.clone()),
            value => {
                let value = number(value)?;
                CounterKey::Number(if value == 0.0 { 0 } else { value.to_bits() })
            }
        };
        let entry = counts.entry(key).or_insert((0, index));
        entry.0 += 1;
    }
    let index = counts
        .values()
        .max_by_key(|(count, index)| (*count, std::cmp::Reverse(*index)))
        .map(|(_, index)| *index)
        .ok_or(MetricAggregateError::Type)?;
    Ok(samples[index].value.clone())
}

fn median(samples: &[MetricSample], ordinal: bool) -> Result<Value, MetricAggregateError> {
    let mut values: Vec<_> = samples.iter().map(|sample| &sample.value).collect();
    if values.iter().all(|value| value.is_string()) {
        values.sort_by(|first, second| first.as_str().cmp(&second.as_str()));
    } else {
        let numeric: Vec<_> = values
            .iter()
            .map(|value| number(value))
            .collect::<Result<_, _>>()?;
        let mut indices: Vec<_> = (0..values.len()).collect();
        indices.sort_by(|first, second| {
            numeric[*first]
                .partial_cmp(&numeric[*second])
                .unwrap_or(Ordering::Equal)
        });
        values = indices
            .into_iter()
            .map(|index| &samples[index].value)
            .collect();
    }
    let middle = values.len() / 2;
    if ordinal || !values.len().is_multiple_of(2) {
        return Ok(values[(values.len() - 1) / 2].clone());
    }
    finite(f64::midpoint(
        number(values[middle - 1])?,
        number(values[middle])?,
    ))
}

fn delta(
    samples: &[MetricSample],
    predecessor: Option<&Value>,
) -> Result<Value, MetricAggregateError> {
    let values: Vec<_> = predecessor
        .into_iter()
        .chain(samples.iter().map(|sample| &sample.value))
        .collect();
    if values.len() < 2 {
        return Ok(Value::Null);
    }
    let mut deltas = Vec::with_capacity(values.len() - 1);
    for pair in values.windows(2) {
        if compare(pair[1], pair[0])? == Ordering::Less {
            number(pair[1])?;
            deltas.push(pair[1].clone());
        } else {
            let negative = multiply_numbers(pair[0], &Value::from(-1))
                .map_err(|_| MetricAggregateError::Type)?;
            deltas.push(sum([pair[1], &negative])?);
        }
    }
    sum(deltas.iter())
}

#[expect(
    clippy::cast_precision_loss,
    reason = "Sample counts are denominators in floating-point aggregates"
)]
pub(super) fn calculate(
    samples: &[MetricSample],
    value_kind: &str,
    method: &str,
    predecessor: Option<&Value>,
) -> Result<Value, MetricAggregateError> {
    if samples.is_empty() {
        return Ok(Value::Null);
    }
    match method {
        "sum" | "mean" => {
            for sample in samples {
                number(&sample.value)?;
            }
            let total = sum(samples.iter().map(|sample| &sample.value))?;
            if method == "mean" {
                finite(number(&total)? / samples.len() as f64)
            } else {
                Ok(total)
            }
        }
        "min" | "max" => {
            let mut result = &samples[0].value;
            for sample in &samples[1..] {
                let order = compare(&sample.value, result)?;
                if (method == "min" && order == Ordering::Less)
                    || (method == "max" && order == Ordering::Greater)
                {
                    result = &sample.value;
                }
            }
            Ok(result.clone())
        }
        "latest" => Ok(samples[samples.len() - 1].value.clone()),
        "median" => median(samples, value_kind == "ordinal"),
        "distribution" | "counts" => counts(samples),
        "mode" => mode(samples),
        "count_true" => Ok(Value::from(
            samples
                .iter()
                .filter(|sample| sample.value == Value::Bool(true))
                .count(),
        )),
        "rate" => finite(
            samples
                .iter()
                .filter(|sample| sample.value == Value::Bool(true))
                .count() as f64
                / samples.len() as f64,
        ),
        "delta" => delta(samples, predecessor),
        _ => Ok(Value::Null),
    }
}
