use serde_json::Value;

use super::{InvalidAnalysisSpec, document_hash};
use crate::event_time::parse_aware_datetime;

/// A calculation's model copy after its original specification was validated.
pub struct DerivedAnalysisSpec {
    document: Value,
    hash: String,
    start: i64,
    end: i64,
    cutoff: i64,
}

impl DerivedAnalysisSpec {
    pub(super) fn new(
        document: Value,
        start: i64,
        end: i64,
        cutoff: i64,
    ) -> Result<Self, InvalidAnalysisSpec> {
        let hash = document_hash(&document)?;
        Ok(Self {
            document,
            hash,
            start,
            end,
            cutoff,
        })
    }

    pub fn document(&self) -> &Value {
        &self.document
    }
    pub fn hash(&self) -> &str {
        &self.hash
    }
    pub fn start_microseconds(&self) -> i64 {
        self.start
    }
    pub fn end_microseconds(&self) -> i64 {
        self.end
    }
    pub fn knowledge_cutoff_microseconds(&self) -> i64 {
        self.cutoff
    }

    /// Subtract a gap in the input datetime's fixed offset before converting to UTC.
    pub fn start_before_seconds(&self, seconds: i64) -> Result<i64, InvalidAnalysisSpec> {
        let parsed =
            parse_aware_datetime(&self.document["start"]).ok_or(InvalidAnalysisSpec::Validation)?;
        let offset = parsed
            .time
            .tz_offset
            .ok_or(InvalidAnalysisSpec::Validation)?;
        let delta = i128::from(seconds) * 1_000_000;
        let shifted = i128::from(self.start) - delta;
        let local = shifted + i128::from(offset) * 1_000_000;
        if !(-62_135_596_800_000_000..=253_402_300_799_999_999).contains(&local) {
            return Err(InvalidAnalysisSpec::CalendarOverflow);
        }
        i64::try_from(shifted).map_err(|_| InvalidAnalysisSpec::CalendarOverflow)
    }
}

#[cfg(test)]
mod tests {
    use std::error::Error;

    use serde_json::Value;

    use super::DerivedAnalysisSpec;
    use crate::analysis_spec::AnalysisSpec;

    fn compare(
        actual: &DerivedAnalysisSpec,
        expected: &Value,
        index: usize,
    ) -> Result<(), Box<dyn Error>> {
        assert_eq!(actual.document(), &expected["document"], "document {index}");
        assert_eq!(
            Some(actual.hash()),
            expected["hash"].as_str(),
            "hash {index}"
        );
        assert_eq!(
            Some(actual.start_microseconds()),
            expected["start"].as_i64(),
            "start {index}"
        );
        assert_eq!(
            Some(actual.end_microseconds()),
            expected["end"].as_i64(),
            "end {index}"
        );
        assert_eq!(
            Some(actual.knowledge_cutoff_microseconds()),
            expected["cutoff"].as_i64(),
            "cutoff {index}"
        );
        for (seconds, expected) in expected["shifts"].as_object().ok_or("Missing shifts")? {
            let actual = actual.start_before_seconds(seconds.parse()?);
            if let Some(error) = expected["error"].as_str() {
                assert_eq!(
                    actual.err().map(|error| error.class_name()),
                    Some(error),
                    "shift {index} {seconds}"
                );
            } else {
                assert_eq!(
                    Some(actual?),
                    expected["value"].as_i64(),
                    "shift {index} {seconds}"
                );
            }
        }
        Ok(())
    }

    #[test]
    fn matches_frozen_model_copies_and_calendar_shifts() -> Result<(), Box<dyn Error>> {
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../../contracts/derived-analysis-specs.json"))?;
        for (index, case) in cases.iter().enumerate() {
            let original = AnalysisSpec::from_json(&case["input"])?;
            compare(&original.derive_aggregate()?, &case["primary"], index)?;
            if let Some(expected) = case.get("comparison") {
                compare(&original.derive_comparison_aggregate()?, expected, index)?;
            }
        }
        Ok(())
    }
}
