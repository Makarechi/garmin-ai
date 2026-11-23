//! Calculations over ordered metric samples selected at one knowledge cutoff.

mod coverage;
mod values;

use std::{error::Error, fmt};

use serde_json::Value;

#[derive(Debug)]
pub struct MetricSample {
    pub value: Value,
    /// The effective interval start, or the sample's observation time.
    pub interval_start: i64,
    /// The end selected from recorded bounds, the next sample and the maximum gap.
    pub interval_end: Option<i64>,
    pub coverage: Option<f64>,
}

#[derive(Debug)]
pub struct MetricAggregate {
    pub value: Value,
    pub coverage_ratio: Option<f64>,
}

#[derive(Clone, Copy, Debug)]
pub enum MetricAggregateError {
    Type,
    Nonfinite,
    Window,
}

impl MetricAggregateError {
    #[must_use]
    pub fn class_name(self) -> &'static str {
        match self {
            Self::Type => "TypeError",
            Self::Nonfinite | Self::Window => "ValueError",
        }
    }
}

impl fmt::Display for MetricAggregateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Type => "Metric values are incompatible with the selected calculation",
            Self::Nonfinite => "Metric calculation produced a nonfinite value",
            Self::Window => "Metric calculation requires a positive bounded window",
        })
    }
}
impl Error for MetricAggregateError {}

/// Apply the retained metric version's method and coverage policy to selected samples.
/// Selection, revision precedence and interval-end reconstruction belong to the reader.
pub fn aggregate_samples(
    samples: &[MetricSample],
    value_kind: &str,
    method: &str,
    policy: &Value,
    start: i64,
    end: i64,
    predecessor: Option<&Value>,
) -> Result<MetricAggregate, MetricAggregateError> {
    if end <= start || end.checked_sub(start).is_none() {
        return Err(MetricAggregateError::Window);
    }
    let mut result = MetricAggregate {
        value: values::calculate(samples, value_kind, method, predecessor)?,
        coverage_ratio: None,
    };
    if policy["kind"] == "time_weighted" {
        coverage::apply(&mut result, samples, method, policy, start, end)?;
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use std::error::Error;

    use serde_json::Value;

    use super::{MetricSample, aggregate_samples};

    fn compare(expected: &Value, actual: &Value, index: usize, field: &str) {
        if let (Some(expected), Some(actual)) = (expected.as_f64(), actual.as_f64()) {
            assert_eq!(
                expected.to_bits(),
                actual.to_bits(),
                "case {index} {field}: {expected} versus {actual}"
            );
        } else {
            assert_eq!(expected, actual, "case {index} {field}");
        }
    }

    #[test]
    fn matches_frozen_metric_calculations() -> Result<(), Box<dyn Error>> {
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../contracts/metric-aggregates.json"))?;
        for (index, case) in cases.iter().enumerate() {
            let samples: Vec<MetricSample> = case["samples"]
                .as_array()
                .ok_or("Missing samples")?
                .iter()
                .map(|sample| {
                    Ok(MetricSample {
                        value: sample["value"].clone(),
                        interval_start: sample["interval_start"]
                            .as_i64()
                            .ok_or("Missing sample start")?,
                        interval_end: sample["interval_end"].as_i64(),
                        coverage: sample["coverage"].as_f64(),
                    })
                })
                .collect::<Result<_, Box<dyn Error>>>()?;
            let actual = aggregate_samples(
                &samples,
                case["value_kind"].as_str().ok_or("Missing value kind")?,
                case["method"].as_str().ok_or("Missing method")?,
                &case["policy"],
                case["start"].as_i64().ok_or("Missing start")?,
                case["end"].as_i64().ok_or("Missing end")?,
                case.get("predecessor").filter(|value| !value.is_null()),
            );
            if let Some(expected) = case["error"].as_str() {
                assert_eq!(
                    actual.err().map(super::MetricAggregateError::class_name),
                    Some(expected),
                    "case {index}"
                );
            } else {
                let actual = actual.map_err(|error| format!("case {index}: {error}"))?;
                compare(&case["expected"]["value"], &actual.value, index, "value");
                compare(
                    &case["expected"]["coverage_ratio"],
                    &serde_json::to_value(actual.coverage_ratio)?,
                    index,
                    "coverage_ratio",
                );
            }
        }
        if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
            for line in status
                .lines()
                .filter(|line| line.starts_with("VmHWM:") || line.starts_with("VmRSS:"))
            {
                eprintln!("{line}");
            }
            for name in ["memory.peak", "memory.swap.peak", "memory.events"] {
                if let Ok(value) = std::fs::read_to_string(format!("/sys/fs/cgroup/{name}")) {
                    eprintln!("{name}\n{value}");
                }
            }
        }
        Ok(())
    }
}
