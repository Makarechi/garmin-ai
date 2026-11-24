use std::{error::Error, fmt};

use serde_json::{Value, json};

use crate::timestamp::StoredInstant;

const MAX_INTERVAL_MICROSECONDS: i64 = 31 * 24 * 60 * 60 * 1_000_000;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HealthReplacement {
    start: StoredInstant,
    end: StoredInstant,
    metrics: Vec<String>,
    evidence: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvalidHealthReplacement {
    Format,
    Bounds,
    Metrics,
    Evidence,
}

impl fmt::Display for InvalidHealthReplacement {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Format => "Replacement evidence could not be read",
            Self::Bounds => "Replacement interval must be positive and at most 31 days",
            Self::Metrics => "Replacement channels do not match endpoint",
            Self::Evidence => "Adapter completeness evidence is required",
        })
    }
}

impl Error for InvalidHealthReplacement {}

impl HealthReplacement {
    pub fn new(
        endpoint: &str,
        start: StoredInstant,
        end: StoredInstant,
        mut metrics: Vec<String>,
        evidence: String,
    ) -> Result<Self, InvalidHealthReplacement> {
        let duration = end.unix_microseconds() - start.unix_microseconds();
        if !(1..=MAX_INTERVAL_MICROSECONDS).contains(&duration) {
            return Err(InvalidHealthReplacement::Bounds);
        }
        metrics.sort_unstable();
        metrics.dedup();
        let replacement = Self {
            start,
            end,
            metrics,
            evidence,
        };
        replacement.validate_endpoint(endpoint)?;
        if !(1..=200).contains(&replacement.evidence.chars().count()) {
            return Err(InvalidHealthReplacement::Evidence);
        }
        Ok(replacement)
    }

    pub fn restore(
        endpoint: &str,
        value: &Value,
    ) -> Result<Option<Self>, InvalidHealthReplacement> {
        let absent = match value {
            Value::Null | Value::Bool(false) => true,
            Value::String(value) => value.is_empty(),
            Value::Number(value) => value.as_f64() == Some(0.0),
            Value::Array(value) => value.is_empty(),
            Value::Object(value) => value.is_empty(),
            Value::Bool(true) => false,
        };
        if absent {
            return Ok(None);
        }
        let stamp = |field| {
            value[field]
                .as_str()
                .and_then(|time| time.parse().ok())
                .ok_or(InvalidHealthReplacement::Format)
        };
        let metrics = value["metrics"]
            .as_array()
            .and_then(|metrics| {
                metrics
                    .iter()
                    .map(|item| item.as_str().map(str::to_owned))
                    .collect()
            })
            .ok_or(InvalidHealthReplacement::Format)?;
        let evidence = value["evidence"]
            .as_str()
            .ok_or(InvalidHealthReplacement::Format)?
            .to_owned();
        Self::new(endpoint, stamp("start")?, stamp("end")?, metrics, evidence).map(Some)
    }

    pub fn validate_endpoint(&self, endpoint: &str) -> Result<(), InvalidHealthReplacement> {
        let expected = replacement_channels(endpoint);
        if self.metrics.is_empty()
            || self
                .metrics
                .iter()
                .any(|metric| !expected.contains(&metric.as_str()))
        {
            return Err(InvalidHealthReplacement::Metrics);
        }
        Ok(())
    }

    #[must_use]
    pub fn start(&self) -> StoredInstant {
        self.start
    }

    #[must_use]
    pub fn end(&self) -> StoredInstant {
        self.end
    }

    #[must_use]
    pub fn metrics(&self) -> &[String] {
        &self.metrics
    }

    #[must_use]
    pub fn document(&self) -> Value {
        json!({
            "start": self.start.to_string(), "end": self.end.to_string(),
            "metrics": self.metrics, "evidence": self.evidence,
        })
    }
}

#[must_use]
pub fn replacement_channels(endpoint: &str) -> &[&str] {
    match endpoint {
        "heart_rate" => &["heart_rate_bpm"],
        "stress" => &["stress_score", "body_battery"],
        "hrv" => &["hrv_rmssd_ms"],
        "respiration" => &["respiration_rpm"],
        "spo2" => &["spo2_pct"],
        "steps" => &["steps_bucket"],
        _ => &[],
    }
}
