use super::{mean, quantile};

const BLOCK_SIZE: usize = 7;
const RESAMPLES: usize = 1000;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MeanDifference {
    pub difference: Option<f64>,
    pub ci95: Option<[f64; 2]>,
    pub method: &'static str,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PairedDifference {
    pub ci95: Option<[f64; 2]>,
    pub sign_permutation_p: Option<f64>,
}

/// Reproduce the migraine comparison's consecutive seeded bootstrap and sign draws.
pub fn paired_mean_difference(differences: &[f64]) -> PairedDifference {
    if differences.len() < 10 {
        return PairedDifference {
            ci95: None,
            sign_permutation_p: None,
        };
    }
    let mut stream = BootstrapRandom::seed_42();
    let mut sample = vec![0.0; differences.len()];
    let mut distribution = [0.0; 2000];
    for result in &mut distribution {
        for value in &mut sample {
            *value = differences[stream.below(differences.len())];
        }
        *result = mean(&sample);
    }
    distribution.sort_unstable_by(f64::total_cmp);
    let observed = mean(differences).abs();
    let mut extreme = 0_u32;
    for _ in 0..2000 {
        for (value, &difference) in sample.iter_mut().zip(differences) {
            let sign = if stream.below(2) == 0 { -1.0 } else { 1.0 };
            *value = sign * difference;
        }
        extreme += u32::from(mean(&sample).abs() >= observed);
    }
    PairedDifference {
        ci95: Some([
            quantile(&distribution, 0.025),
            quantile(&distribution, 0.975),
        ]),
        sign_permutation_p: Some(f64::from(extreme + 1) / 2001.0),
    }
}

/// Reproduce the app's seeded circular block bootstrap using one sample buffer.
/// The supported app queries bound each period before calling this routine.
pub fn block_mean_difference(first: &[f64], second: &[f64]) -> MeanDifference {
    let difference = (!first.is_empty() && !second.is_empty()).then(|| mean(first) - mean(second));
    if first.len().min(second.len()) < 2 * BLOCK_SIZE {
        return MeanDifference {
            difference,
            ci95: None,
            method: "insufficient observations for block resampling",
        };
    }
    let mut stream = BootstrapRandom::seed_42();
    let mut sample = Vec::with_capacity(first.len().max(second.len()));
    let mut distribution = [0.0; RESAMPLES];
    for result in &mut distribution {
        *result = resampled_mean(first, &mut sample, &mut stream);
    }
    for result in &mut distribution {
        *result -= resampled_mean(second, &mut sample, &mut stream);
    }
    distribution.sort_unstable_by(f64::total_cmp);
    MeanDifference {
        difference,
        ci95: Some([
            quantile(&distribution, 0.025),
            quantile(&distribution, 0.975),
        ]),
        method: "7-observation circular block bootstrap; 1000 resamples; seed 42",
    }
}

fn resampled_mean(values: &[f64], sample: &mut Vec<f64>, stream: &mut BootstrapRandom) -> f64 {
    sample.clear();
    for block in (0..values.len()).step_by(BLOCK_SIZE) {
        let start = stream.below(values.len());
        let length = BLOCK_SIZE.min(values.len() - block);
        let end = start + length;
        sample.extend_from_slice(&values[start..end.min(values.len())]);
        if end > values.len() {
            sample.extend_from_slice(&values[..end - values.len()]);
        }
    }
    mean(sample)
}

struct BootstrapRandom {
    state: u128,
    saved_word: Option<u32>,
}

impl BootstrapRandom {
    // NumPy 2.5.3 default_rng(42).bit_generator.state. The application fixes this seed.
    fn seed_42() -> Self {
        Self {
            state: 274_674_114_334_540_486_603_088_602_300_644_985_544,
            saved_word: None,
        }
    }

    #[expect(
        clippy::cast_possible_truncation,
        reason = "The pinned PCG stream extracts the low bits of the state and output words"
    )]
    fn next_word(&mut self) -> u32 {
        if let Some(word) = self.saved_word.take() {
            return word;
        }
        const MULTIPLIER: u128 = 47_026_247_687_942_121_848_144_207_491_837_523_525;
        const INCREMENT: u128 = 332_724_090_758_049_132_448_979_897_138_935_081_983;
        self.state = self.state.wrapping_mul(MULTIPLIER).wrapping_add(INCREMENT);
        let upper = (self.state >> 64) as u64;
        let lower = self.state as u64;
        let bits = (upper ^ lower).rotate_right((upper >> 58) as u32);
        self.saved_word = Some((bits >> 32) as u32);
        bits as u32
    }

    #[expect(
        clippy::cast_possible_truncation,
        reason = "The bounded NumPy sampler uses the low word of the product for rejection"
    )]
    fn below(&mut self, length: usize) -> usize {
        // The caller's database date window is bounded to 3661 observations.
        let length = length as u64;
        let remainder = (1_u64 << 32) % length;
        loop {
            let product = u64::from(self.next_word()) * length;
            if u64::from(product as u32) >= remainder {
                return (product >> 32) as usize;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::BootstrapRandom;

    #[test]
    fn uses_the_pinned_numpy_bounded_integer_stream() {
        let mut stream = BootstrapRandom::seed_42();
        assert_eq!(
            std::array::from_fn(|_| stream.below(14)),
            [1, 10, 9, 6, 6, 12, 1, 9, 2, 1, 7, 13],
        );
        assert_eq!(
            std::array::from_fn(|_| stream.below(367)),
            [270, 279, 263, 288, 188, 47, 308, 165, 183, 136, 66, 340],
        );
    }
}
