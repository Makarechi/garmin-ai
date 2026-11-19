mod derived;
mod source;

pub use derived::DerivedAnalysisSpec;

use std::{error::Error, fmt};

use serde_json::{Map, Value, json};
use time::{OffsetDateTime, UtcOffset};

use crate::{
    event_contract::python_ascii_json, event_input::known_timezone,
    event_time::parse_aware_datetime, legacy_json::integer_argument, sha256_hex,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AnalysisOperation {
    QueryEntries,
    QueryObservations,
    AggregateMetric,
    ComparePeriods,
    QueryCompleteness,
    ThresholdSegments,
    ThresholdByLocalHour,
}

impl AnalysisOperation {
    fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "query_entries" => Self::QueryEntries,
            "query_observations" => Self::QueryObservations,
            "aggregate_metric" => Self::AggregateMetric,
            "compare_periods" => Self::ComparePeriods,
            "query_completeness" => Self::QueryCompleteness,
            "threshold_segments" => Self::ThresholdSegments,
            "threshold_by_local_hour" => Self::ThresholdByLocalHour,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, Debug)]
pub enum InvalidAnalysisSpec {
    Validation,
    CalendarOverflow,
}

impl InvalidAnalysisSpec {
    pub fn class_name(self) -> &'static str {
        match self {
            Self::Validation => "ValidationError",
            Self::CalendarOverflow => "OverflowError",
        }
    }
}

impl fmt::Display for InvalidAnalysisSpec {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Validation => "Generic analysis arguments are invalid",
            Self::CalendarOverflow => "Generic analysis UTC conversion exceeds the calendar",
        })
    }
}
impl Error for InvalidAnalysisSpec {}

pub struct AnalysisSpec {
    document: Value,
    hash: String,
    operation: AnalysisOperation,
    start: OffsetDateTime,
    end: OffsetDateTime,
    cutoff: i64,
    comparison: Option<(i64, i64)>,
}

impl AnalysisSpec {
    /// Normalize the frozen typed specification before resolving any stored contracts.
    pub fn from_json(input: &Value) -> Result<Self, InvalidAnalysisSpec> {
        let fields = input.as_object().ok_or(InvalidAnalysisSpec::Validation)?;
        if fields.keys().any(|key| {
            !matches!(
                key.as_str(),
                "operation"
                    | "metric_key"
                    | "definition_key"
                    | "start"
                    | "end"
                    | "comparison_start"
                    | "comparison_end"
                    | "method"
                    | "metric_version"
                    | "source"
                    | "limit"
                    | "knowledge_cutoff"
                    | "time_relation"
                    | "threshold"
                    | "threshold_operator"
                    | "timezone"
            )
        }) {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let name = fields
            .get("operation")
            .and_then(Value::as_str)
            .ok_or(InvalidAnalysisSpec::Validation)?;
        let operation = AnalysisOperation::parse(name).ok_or(InvalidAnalysisSpec::Validation)?;
        let metric = text(fields, "metric_key")?;
        let definition = text(fields, "definition_key")?;
        if metric
            .into_iter()
            .chain(definition)
            .any(|key| !valid_key(key))
        {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let method = text(fields, "method")?;
        if method.is_some_and(|method| {
            method.is_empty()
                || method.len() > 32
                || !method
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte == b'_')
        }) {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let raw_source = text(fields, "source")?;
        if raw_source.is_some_and(|source| source.chars().count() > 200) {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let timezone = text(fields, "timezone")?;
        if timezone.is_some_and(|zone| zone.is_empty() || zone.chars().count() > 100) {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let (start, start_json) =
            timestamp(fields.get("start").ok_or(InvalidAnalysisSpec::Validation)?)?;
        let (end, end_json) = timestamp(fields.get("end").ok_or(InvalidAnalysisSpec::Validation)?)?;
        let (cutoff, cutoff_json) = timestamp(
            fields
                .get("knowledge_cutoff")
                .ok_or(InvalidAnalysisSpec::Validation)?,
        )?;
        let comparison_start = fields
            .get("comparison_start")
            .filter(|value| !value.is_null())
            .map(timestamp)
            .transpose()?;
        let comparison_end = fields
            .get("comparison_end")
            .filter(|value| !value.is_null())
            .map(timestamp)
            .transpose()?;
        let relation = match fields.get("time_relation") {
            None => "starts_within",
            Some(Value::String(value)) if matches!(value.as_str(), "starts_within" | "overlap") => {
                value
            }
            _ => return Err(InvalidAnalysisSpec::Validation),
        };
        let threshold = match fields.get("threshold") {
            None | Some(Value::Null) => None,
            Some(Value::Number(number)) => {
                let value = if number.as_i64() == Some(0) {
                    0.0
                } else {
                    number.as_f64().ok_or(InvalidAnalysisSpec::Validation)?
                };
                if !value.is_finite() {
                    return Err(InvalidAnalysisSpec::Validation);
                }
                Some(value)
            }
            _ => return Err(InvalidAnalysisSpec::Validation),
        };
        let operator = text(fields, "threshold_operator")?;
        if operator.is_some_and(|operator| !matches!(operator, "gt" | "gte" | "lt" | "lte")) {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let version = match fields.get("metric_version") {
            None | Some(Value::Null) => Value::Null,
            Some(value) => {
                let number = integer_argument(value).ok_or(InvalidAnalysisSpec::Validation)?;
                if number <= 0.into() {
                    return Err(InvalidAnalysisSpec::Validation);
                }
                serde_json::from_str(&number.to_string())
                    .map_err(|_| InvalidAnalysisSpec::Validation)?
            }
        };
        let limit: u16 = match fields.get("limit") {
            None => 500,
            Some(value) => integer_argument(value)
                .ok_or(InvalidAnalysisSpec::Validation)?
                .try_into()
                .map_err(|_| InvalidAnalysisSpec::Validation)?,
        };
        if !(1..=1000).contains(&limit) {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let start = OffsetDateTime::from_unix_timestamp_nanos(i128::from(start) * 1000)
            .map_err(|_| InvalidAnalysisSpec::CalendarOverflow)?;
        let end = OffsetDateTime::from_unix_timestamp_nanos(i128::from(end) * 1000)
            .map_err(|_| InvalidAnalysisSpec::CalendarOverflow)?;
        let duration = end - start;
        if !(1..=9999).contains(&start.to_offset(UtcOffset::UTC).year())
            || !(1..=9999).contains(&end.to_offset(UtcOffset::UTC).year())
        {
            return Err(InvalidAnalysisSpec::CalendarOverflow);
        }
        if !duration.is_positive() || duration > time::Duration::days(366) {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let comparison = match (
            operation,
            comparison_start.as_ref(),
            comparison_end.as_ref(),
        ) {
            (AnalysisOperation::ComparePeriods, Some((first, _)), Some((last, _)))
                if first < last
                    && i128::from(*last - *first)
                        <= time::Duration::days(366).whole_microseconds() =>
            {
                Some((*first, *last))
            }
            (AnalysisOperation::ComparePeriods, _, _) => {
                return Err(InvalidAnalysisSpec::Validation);
            }
            (_, None, None) => None,
            _ => return Err(InvalidAnalysisSpec::Validation),
        };
        if relation != "starts_within" && operation != AnalysisOperation::QueryEntries {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let threshold_operation = matches!(
            operation,
            AnalysisOperation::ThresholdSegments | AnalysisOperation::ThresholdByLocalHour
        );
        if threshold_operation {
            if threshold.is_none()
                || operator.is_none()
                || method.is_some()
                || duration > time::Duration::days(31)
            {
                return Err(InvalidAnalysisSpec::Validation);
            }
        } else if threshold.is_some() || operator.is_some() {
            return Err(InvalidAnalysisSpec::Validation);
        }
        if operation == AnalysisOperation::ThresholdByLocalHour {
            if !timezone.is_some_and(known_timezone) {
                return Err(InvalidAnalysisSpec::Validation);
            }
        } else if timezone.is_some() {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let source = raw_source.map(canonical_source).transpose()?;
        if operation == AnalysisOperation::QueryEntries {
            if definition.is_none() || source.is_some() {
                return Err(InvalidAnalysisSpec::Validation);
            }
        } else if metric.is_none() {
            return Err(InvalidAnalysisSpec::Validation);
        }
        let document = json!({
            "operation":name,"metric_key":metric,"definition_key":definition,
            "start":start_json,"end":end_json,
            "comparison_start":comparison_start.map(|(_,text)| text),
            "comparison_end":comparison_end.map(|(_,text)| text),
            "method":method,"metric_version":version,"source":source,"limit":limit,
            "knowledge_cutoff":cutoff_json,"time_relation":relation,"threshold":threshold,
            "threshold_operator":operator,"timezone":timezone,
        });
        let hash = document_hash(&document)?;
        Ok(Self {
            document,
            hash,
            operation,
            start,
            end,
            cutoff,
            comparison,
        })
    }

    pub fn document(&self) -> &Value {
        &self.document
    }
    pub fn hash(&self) -> &str {
        &self.hash
    }
    pub fn operation(&self) -> AnalysisOperation {
        self.operation
    }
    pub fn start(&self) -> OffsetDateTime {
        self.start
    }
    pub fn end(&self) -> OffsetDateTime {
        self.end
    }
    pub fn knowledge_cutoff_microseconds(&self) -> i64 {
        self.cutoff
    }
    pub fn comparison_microseconds(&self) -> Option<(i64, i64)> {
        self.comparison
    }

    /// Keep the normalized fields used by Python's aggregate model copy.
    pub fn derive_aggregate(&self) -> Result<DerivedAnalysisSpec, InvalidAnalysisSpec> {
        let mut document = self.document.clone();
        document["operation"] = Value::from("aggregate_metric");
        DerivedAnalysisSpec::new(
            document,
            i64::try_from(self.start.unix_timestamp_nanos() / 1000)
                .map_err(|_| InvalidAnalysisSpec::CalendarOverflow)?,
            i64::try_from(self.end.unix_timestamp_nanos() / 1000)
                .map_err(|_| InvalidAnalysisSpec::CalendarOverflow)?,
            self.cutoff,
        )
    }

    /// Preserve comparison dates whose UTC position crosses the storage calendar.
    pub fn derive_comparison_aggregate(&self) -> Result<DerivedAnalysisSpec, InvalidAnalysisSpec> {
        let (start, end) = self.comparison.ok_or(InvalidAnalysisSpec::Validation)?;
        let mut document = self.document.clone();
        document["operation"] = Value::from("aggregate_metric");
        document["start"] = document["comparison_start"].clone();
        document["end"] = document["comparison_end"].clone();
        document["comparison_start"] = Value::Null;
        document["comparison_end"] = Value::Null;
        DerivedAnalysisSpec::new(document, start, end, self.cutoff)
    }
}

fn document_hash(document: &Value) -> Result<String, InvalidAnalysisSpec> {
    let mut hash_document = document.clone();
    let hash_fields = hash_document
        .as_object_mut()
        .ok_or(InvalidAnalysisSpec::Validation)?;
    for key in ["threshold", "threshold_operator", "timezone"] {
        if hash_fields.get(key) == Some(&Value::Null) {
            hash_fields.remove(key);
        }
    }
    let encoded = python_ascii_json(&hash_document).map_err(|_| InvalidAnalysisSpec::Validation)?;
    Ok(sha256_hex(encoded.as_bytes()))
}

fn text<'a>(
    fields: &'a Map<String, Value>,
    key: &str,
) -> Result<Option<&'a str>, InvalidAnalysisSpec> {
    match fields.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value)),
        _ => Err(InvalidAnalysisSpec::Validation),
    }
}

fn valid_key(key: &str) -> bool {
    let Some(value) = key
        .strip_prefix("system.")
        .or_else(|| key.strip_prefix("user."))
    else {
        return false;
    };
    !value.is_empty()
        && value.len() <= 127
        && value.as_bytes()[0].is_ascii_lowercase()
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(&byte)
        })
}

fn canonical_source(source: &str) -> Result<String, InvalidAnalysisSpec> {
    if source == "event"
        || source
            .strip_prefix("measurement:")
            .is_some_and(|value| !value.is_empty())
    {
        return Ok(source.to_owned());
    }
    let pair = source
        .strip_prefix("observation:")
        .ok_or(InvalidAnalysisSpec::Validation)?;
    Ok(format!("observation:{}", source::canonical_pair(pair)?))
}

fn timestamp(value: &Value) -> Result<(i64, String), InvalidAnalysisSpec> {
    let parsed = parse_aware_datetime(value).ok_or(InvalidAnalysisSpec::Validation)?;
    // The typed cutoff can cross the UTC storage calendar while its local date stays valid.
    let micros =
        i128::from(parsed.timestamp_tz()) * 1_000_000 + i128::from(parsed.time.microsecond);
    Ok((
        i64::try_from(micros).map_err(|_| InvalidAnalysisSpec::Validation)?,
        parsed.to_string(),
    ))
}

#[cfg(test)]
mod tests {
    use super::AnalysisSpec;
    use serde_json::{Value, json};
    use std::error::Error;

    #[test]
    fn matches_frozen_typed_arguments_and_hashes() -> Result<(), Box<dyn Error>> {
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../contracts/analysis-specs.json"))?;
        for (index, case) in cases.iter().enumerate() {
            let actual = AnalysisSpec::from_json(&case["input"]);
            if let Some(error) = case.get("error") {
                assert_eq!(
                    actual.err().map(|error| error.class_name()),
                    error.as_str(),
                    "error class {index} input {}",
                    case["input"]
                );
            } else {
                let actual =
                    actual.map_err(|_| format!("rejected case {index} input {}", case["input"]))?;
                assert_eq!(
                    actual.document(),
                    &case["normalized"],
                    "normalization {index}"
                );
                assert_eq!(Some(actual.hash()), case["hash"].as_str(), "hash {index}");
                let instants = json!({
                    "start":actual.start().unix_timestamp_nanos()/1000,
                    "end":actual.end().unix_timestamp_nanos()/1000,
                    "cutoff":actual.knowledge_cutoff_microseconds(),
                    "comparison":actual.comparison_microseconds(),
                });
                assert_eq!(instants, case["instants"], "instants {index}");
                assert_eq!(
                    AnalysisSpec::from_json(actual.document())?.hash(),
                    actual.hash(),
                    "idempotent normalization {index}"
                );
            }
        }
        Ok(())
    }
}
