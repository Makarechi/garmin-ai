use serde_json::Value;

use super::{MetricAggregate, MetricAggregateError, MetricSample, values};

#[expect(
    clippy::cast_precision_loss,
    reason = "Coverage intervals are reported as fractional seconds"
)]
fn seconds(start: i64, end: i64) -> f64 {
    (end - start) as f64 / 1_000_000.0
}

pub(super) fn apply(
    result: &mut MetricAggregate,
    samples: &[MetricSample],
    method: &str,
    policy: &Value,
    start: i64,
    end: i64,
) -> Result<(), MetricAggregateError> {
    let mut intervals = Vec::with_capacity(samples.len());
    let mut weights = Vec::with_capacity(samples.len());
    for sample in samples {
        let left = sample.interval_start.max(start);
        let right = sample
            .interval_end
            .ok_or(MetricAggregateError::Type)?
            .min(end);
        weights.push(Value::from(seconds(left, right).max(0.0)));
        if left < right {
            intervals.push((left, right));
        }
    }
    intervals.sort_unstable();
    let mut merged: Vec<(i64, i64)> = Vec::with_capacity(intervals.len());
    for (left, right) in intervals {
        if let Some(previous) = merged.last_mut()
            && left <= previous.1
        {
            previous.1 = previous.1.max(right);
        } else {
            merged.push((left, right));
        }
    }
    let durations: Vec<_> = merged
        .iter()
        .map(|(left, right)| Value::from(seconds(*left, *right)))
        .collect();
    let covered = values::number(&values::sum(durations.iter())?)?;
    let mut ratio = (covered / seconds(start, end)).min(1.0);
    for sample in samples {
        if let Some(coverage) = sample.coverage
            && coverage < ratio
        {
            ratio = coverage;
        }
    }
    result.coverage_ratio = Some(ratio);
    if matches!(method, "mean" | "rate") && !samples.is_empty() {
        let denominator = values::number(&values::sum(weights.iter())?)?;
        let mut weighted = Vec::with_capacity(samples.len());
        for (sample, weight) in samples.iter().zip(&weights) {
            let value = if method == "rate" {
                f64::from(u8::from(sample.value == Value::Bool(true)))
            } else {
                values::number(&sample.value)?
            };
            weighted.push(values::finite(values::number(weight)? * value)?);
        }
        result.value = if denominator == 0.0 {
            Value::Null
        } else {
            values::finite(values::number(&values::sum(weighted.iter())?)? / denominator)?
        };
    }
    let mut previous = start;
    let mut largest_gap = 0.0_f64;
    for (left, right) in merged {
        largest_gap = largest_gap.max(seconds(previous, left));
        previous = right;
    }
    largest_gap = largest_gap.max(seconds(previous, end));
    let minimum = policy["minimum_ratio"]
        .as_f64()
        .ok_or(MetricAggregateError::Type)?;
    let maximum_gap = policy["max_gap_seconds"]
        .as_f64()
        .ok_or(MetricAggregateError::Type)?;
    if ratio < minimum || largest_gap > maximum_gap {
        result.value = Value::Null;
    }
    Ok(())
}
