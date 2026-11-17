mod bootstrap;

pub use bootstrap::{MeanDifference, block_mean_difference};

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Description {
    pub n: usize,
    pub mean: Option<f64>,
    pub median: Option<f64>,
    pub sd: Option<f64>,
    pub min: Option<f64>,
    pub max: Option<f64>,
}

/// Describe the finite observations. Missing and non-finite inputs stay outside the denominator.
pub fn describe(values: impl IntoIterator<Item = Option<f64>>) -> Description {
    let mut values: Vec<_> = values
        .into_iter()
        .flatten()
        .filter(|value| value.is_finite())
        .collect();
    let n = values.len();
    if n == 0 {
        return Description::default();
    }
    let mean = mean(&values);
    let sd = (n > 1).then(|| {
        let deviations: Vec<_> = values.iter().map(|value| (value - mean).powi(2)).collect();
        (pairwise_sum(&deviations) / (n - 1) as f64).sqrt()
    });
    values.sort_unstable_by(f64::total_cmp);
    let median = if n.is_multiple_of(2) {
        (values[n / 2 - 1] + values[n / 2]) / 2.0
    } else {
        values[n / 2]
    };
    Description {
        n,
        mean: Some(mean),
        median: Some(median),
        sd,
        min: values.first().copied(),
        max: values.last().copied(),
    }
}

pub fn standardized_difference(
    difference: Option<f64>,
    first: Description,
    second: Description,
) -> Option<f64> {
    let deviation_a = first.sd?;
    let deviation_b = second.sd?;
    let pooled = (((first.n - 1) as f64 * deviation_a.powi(2)
        + (second.n - 1) as f64 * deviation_b.powi(2))
        / (first.n + second.n - 2) as f64)
        .sqrt();
    difference
        .filter(|_| pooled > 0.0)
        .map(|difference| difference / pooled)
}

fn mean(values: &[f64]) -> f64 {
    pairwise_sum(values) / values.len() as f64
}

fn pairwise_sum(values: &[f64]) -> f64 {
    match values.len() {
        0..8 => values.iter().fold(-0.0, |sum, value| sum + value),
        8..=128 => {
            let (chunks, tail) = values.as_chunks::<8>();
            let mut lanes = chunks[0];
            for chunk in &chunks[1..] {
                for (lane, value) in lanes.iter_mut().zip(chunk) {
                    *lane += value;
                }
            }
            let sum = ((lanes[0] + lanes[1]) + (lanes[2] + lanes[3]))
                + ((lanes[4] + lanes[5]) + (lanes[6] + lanes[7]));
            tail.iter().fold(sum, |sum, value| sum + value)
        }
        length => {
            let half = (length / 2) & !7;
            let (first, second) = values.split_at(half);
            pairwise_sum(first) + pairwise_sum(second)
        }
    }
}

fn quantile(sorted: &[f64], fraction: f64) -> f64 {
    let position = (sorted.len() - 1) as f64 * fraction;
    let lower = sorted[position.floor() as usize];
    let upper = sorted[position.ceil() as usize];
    let weight = position.fract();
    if weight < 0.5 {
        lower + (upper - lower) * weight
    } else {
        upper - (upper - lower) * (1.0 - weight)
    }
}
