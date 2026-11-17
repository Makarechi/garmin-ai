include!("numeric_ranges.rs");

pub(super) fn is_numeric(character: char) -> bool {
    let point = u32::from(character);
    let index = NUMERIC_RANGES.partition_point(|&(_, last)| last < point);
    NUMERIC_RANGES
        .get(index)
        .is_some_and(|&(first, _)| first <= point)
}

#[cfg(test)]
mod tests {
    use super::{NUMERIC_DIGEST, is_numeric};

    #[test]
    fn matches_pinned_python_across_unicode() {
        let numbers: Vec<_> = (0..=0x10ffff)
            .filter_map(char::from_u32)
            .filter(|&character| is_numeric(character))
            .flat_map(|character| u32::from(character).to_be_bytes())
            .collect();
        assert_eq!(numbers.len() / 4, 2023);
        assert_eq!(crate::sha256_hex(&numbers), NUMERIC_DIGEST);
    }
}
