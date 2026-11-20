use std::collections::BTreeSet;

use ga_domain::{
    analysis_spec::{AnalysisOperation, AnalysisSpec},
    legacy_json::{Utf8Layout, encode_ascii},
    model_consent::ModelCategories,
    sha256_hex,
    timestamp::StoredInstant,
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{GenericAnalysisError, GenericAnalysisReader, GenericAudience, model_read_in};
use crate::share_policy::ShareDestination;

pub const GENERIC_ANALYSIS: &str = "generic_analysis";
const MAX_EVIDENCE_BYTES: usize = 96_000;
const FIVE_MINUTES_MICROSECONDS: i64 = 300_000_000;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GenericAnalysisEvidence {
    generic_arguments: Map<String, Value>,
    request_time: String,
    generic_result_digest: String,
    generic_result_hash: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LegacyGenericDependency {
    arguments: Map<String, Value>,
    request_time: String,
    result_hash: String,
    snapshots: Vec<GenericMetricSnapshot>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GenericMetricSnapshot {
    metric: String,
    metric_version: i64,
    input_revisions: Map<String, Value>,
}

impl GenericAnalysisEvidence {
    pub fn input(arguments: &Map<String, Value>) -> Option<AnalysisSpec> {
        if arguments.len() != 1 {
            return None;
        }
        AnalysisSpec::from_json(arguments.get("spec")?).ok()
    }

    pub fn from_read(value: &Value, request_time: &str) -> Option<Self> {
        if value["tool"] != GENERIC_ANALYSIS || value["result"].get("error").is_some() {
            return None;
        }
        StoredInstant::from_python_isoformat(request_time)?;
        let arguments = value["arguments"].as_object()?;
        Self::input(arguments)?;
        let result = &value["result"];
        Some(Self {
            generic_arguments: arguments.clone(),
            request_time: request_time.to_owned(),
            generic_result_digest: digest(result)?,
            generic_result_hash: result_hash(result, Utf8Layout::Archive)?,
        })
    }

    pub(crate) fn specification(&self) -> Value {
        json!({
            "tool": GENERIC_ANALYSIS,
            "arguments": self.generic_arguments,
            "result_hash": self.generic_result_hash,
        })
    }

    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        owner: Uuid,
        model: ShareDestination<'_>,
        channel: ShareDestination<'_>,
        categories: ModelCategories,
    ) -> Result<Option<BTreeSet<Uuid>>, GenericAnalysisError> {
        let Some((spec, _)) = refreshed_spec(&self.generic_arguments, &self.request_time) else {
            return Ok(None);
        };
        let read = model_read_in(txn, owner, model, channel, categories, &spec).await?;
        Ok(
            (digest(&read.value).as_deref() == Some(&self.generic_result_digest))
                .then_some(read.tracker_versions),
        )
    }
}

impl LegacyGenericDependency {
    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        owner: Uuid,
        model: ShareDestination<'_>,
        channel: ShareDestination<'_>,
    ) -> Result<bool, GenericAnalysisError> {
        let Some((spec, now)) = refreshed_spec(&self.arguments, &self.request_time) else {
            return Ok(false);
        };
        let expected = match spec.operation() {
            AnalysisOperation::QueryEntries
            | AnalysisOperation::QueryObservations
            | AnalysisOperation::QueryCompleteness => 0,
            AnalysisOperation::ComparePeriods => 2,
            AnalysisOperation::AggregateMetric
            | AnalysisOperation::ThresholdSegments
            | AnalysisOperation::ThresholdByLocalHour => 1,
        };
        if self.snapshots.len() != expected {
            return Ok(false);
        }
        let read = model_read_in(
            txn,
            owner,
            model,
            channel,
            ModelCategories::HEALTH_AND_DIARY,
            &spec,
        )
        .await?;
        if digest(&read.value).as_deref() != Some(&self.result_hash) {
            return Ok(false);
        }
        let single = [&read.value];
        let pair = [&read.value["first"], &read.value["second"]];
        let observations: &[&Value] = match expected {
            0 => &[],
            1 => &single,
            _ => &pair,
        };
        if !self.snapshots.iter().zip(observations).all(|(proof, row)| {
            row["metric"] == proof.metric
                && row["metric_version"] == proof.metric_version
                && row["input_revisions"].as_object() == Some(&proof.input_revisions)
        }) {
            return Ok(false);
        }
        if let Some(now) = now.filter(|_| !self.snapshots.is_empty()) {
            let mut reader = GenericAnalysisReader {
                txn: txn.begin().await?,
                audience: GenericAudience::Owner { read_health: true },
            };
            for proof in &self.snapshots {
                if !reader
                    .aggregate_revisions_current(
                        &proof.metric,
                        proof.metric_version,
                        &proof.input_revisions,
                        now,
                    )
                    .await?
                {
                    return Ok(false);
                }
            }
            reader.txn.rollback().await?;
        }
        Ok(true)
    }
}

fn refreshed_spec(
    arguments: &Map<String, Value>,
    request_time: &str,
) -> Option<(AnalysisSpec, Option<StoredInstant>)> {
    let at = StoredInstant::from_python_isoformat(request_time)?;
    let spec = GenericAnalysisEvidence::input(arguments)?;
    if spec.knowledge_cutoff_microseconds() < at.unix_microseconds() - FIVE_MINUTES_MICROSECONDS {
        return Some((spec, None));
    }
    let now = OffsetDateTime::now_utc()
        .unix_timestamp_nanos()
        .div_euclid(1_000);
    let now = StoredInstant::from_unix_microseconds(i64::try_from(now).ok()?).ok()?;
    if now.unix_microseconds() <= spec.knowledge_cutoff_microseconds() {
        return Some((spec, Some(now)));
    }
    let mut updated = spec.document().clone();
    updated["knowledge_cutoff"] = json!(now.to_string());
    Some((AnalysisSpec::from_json(&updated).ok()?, Some(now)))
}

fn digest(value: &Value) -> Option<String> {
    fn remove_volatile(value: &mut Value) {
        if let Some(fields) = value.as_object_mut() {
            fields.remove("spec_hash");
            fields.remove("knowledge_cutoff");
            for key in ["first", "second"] {
                if let Some(child) = fields.get_mut(key) {
                    remove_volatile(child);
                }
            }
        }
    }
    let mut comparable = value.clone();
    remove_volatile(&mut comparable);
    result_hash(&comparable, Utf8Layout::Compact)
}

fn result_hash(value: &Value, layout: Utf8Layout) -> Option<String> {
    let mut ordered = value.clone();
    ordered.sort_all_objects();
    let bytes = serde_json::to_vec(&ordered).ok()?;
    let encoded = encode_ascii(&bytes, layout, MAX_EVIDENCE_BYTES).ok()?;
    Some(sha256_hex(encoded.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_python_comparable_result_and_keeps_row_provenance() {
        let mut result = json!({
            "first": {"spec_hash":"unstable","knowledge_cutoff":"unstable","metric":"user.имя","value":1e-7},
            "second": {"value":2.5,"source":"synthetic"},
            "knowledge_cutoff":"unstable",
            "rows":[{"spec_hash":"retained","text":"🙂"}],
        });
        let first = digest(&result);
        assert_eq!(
            first.as_deref(),
            Some("d6b17fcdff48d3a8cd07186d22692c5bca33454cc3a0ac1988a903d9bf769ba6")
        );
        result["first"]["knowledge_cutoff"] = json!("different");
        assert_eq!(digest(&result), first);
        result["rows"][0]["spec_hash"] = json!("different");
        assert_ne!(digest(&result), first);
    }
}
