use std::collections::BTreeSet;

use ga_domain::{
    analysis_spec::AnalysisSpec,
    legacy_json::{Utf8Layout, encode_ascii},
    model_consent::ModelCategories,
    sha256_hex,
    timestamp::StoredInstant,
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{GenericAnalysisError, model_read_in};
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
        let Some(request_time) = StoredInstant::from_python_isoformat(&self.request_time) else {
            return Ok(None);
        };
        let Some(mut spec) = Self::input(&self.generic_arguments) else {
            return Ok(None);
        };
        if spec.knowledge_cutoff_microseconds()
            >= request_time.unix_microseconds() - FIVE_MINUTES_MICROSECONDS
        {
            let now = OffsetDateTime::now_utc()
                .unix_timestamp_nanos()
                .div_euclid(1_000);
            let Some(now) = i64::try_from(now)
                .ok()
                .and_then(|micros| StoredInstant::from_unix_microseconds(micros).ok())
            else {
                return Ok(None);
            };
            if now.unix_microseconds() > spec.knowledge_cutoff_microseconds() {
                let mut updated = spec.document().clone();
                updated["knowledge_cutoff"] = json!(now.to_string());
                let Ok(refreshed) = AnalysisSpec::from_json(&updated) else {
                    return Ok(None);
                };
                spec = refreshed;
            }
        }
        let read = model_read_in(txn, owner, model, channel, categories, &spec).await?;
        Ok(
            (digest(&read.value).as_deref() == Some(&self.generic_result_digest))
                .then_some(read.tracker_versions),
        )
    }
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
