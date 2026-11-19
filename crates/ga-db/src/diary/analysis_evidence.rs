use std::collections::BTreeSet;

use ga_domain::{
    legacy_json::{Utf8Layout, encode, encode_ascii},
    model_consent::ModelCategories,
    sha256_hex,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{DiaryEventRange, DiaryReadError, analysis_events_in};
use crate::{
    conversation_memory::{AnalysisReadContext, AnalysisSubject, ConversationBinding, MemoryFence},
    health_analysis::owner_timezone,
    model_gate::onboarding::onboarding_allows_model,
    share_policy::{DestinationKind, ShareDestination},
};

#[derive(Deserialize, Serialize)]
pub(crate) struct DiaryAnalysisFence {
    model: String,
    timezone: String,
    configured_timezone: String,
    goals_revision: u64,
    reads: Vec<DiaryReadFence>,
}

#[derive(Deserialize, Serialize)]
struct DiaryReadFence {
    start: String,
    end: String,
    kind: Option<String>,
    digest: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    result_hash: Option<String>,
}

impl DiaryAnalysisFence {
    pub(crate) fn from_evidence(
        model: &str,
        timezone: &str,
        configured_timezone: &str,
        goals_revision: u64,
        evidence: &[Value],
    ) -> Option<Self> {
        Some(Self {
            model: model.to_owned(),
            timezone: timezone.to_owned(),
            configured_timezone: configured_timezone.to_owned(),
            goals_revision,
            reads: evidence
                .iter()
                .map(DiaryReadFence::from_evidence)
                .collect::<Option<_>>()?,
        })
    }

    pub(crate) async fn check(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        binding: ConversationBinding,
        inbound_id: Uuid,
        operation_id: Uuid,
        memory: &MemoryFence,
    ) -> Result<Option<BTreeSet<Uuid>>, sqlx::Error> {
        self.check_subject(
            txn,
            AnalysisReadContext {
                binding,
                inbound_id,
                operation_id,
                memory,
                goals_revision: self.goals_revision,
            }
            .into(),
        )
        .await
    }

    pub(crate) fn model_specs(&self) -> Vec<Value> {
        self.reads
            .iter()
            .map(|read| {
                let mut arguments = json!({"start":read.start,"end":read.end});
                if let Some(kind) = &read.kind {
                    arguments["kind"] = json!(kind);
                }
                json!({
                    "tool":"events",
                    "arguments":arguments,
                    "result_hash":read.result_hash.as_deref().unwrap_or(&read.digest),
                })
            })
            .collect()
    }

    pub(crate) async fn check_web(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        binding: ConversationBinding,
        memory: &MemoryFence,
    ) -> Result<Option<BTreeSet<Uuid>>, sqlx::Error> {
        self.check_subject(
            txn,
            AnalysisSubject::Web {
                binding,
                memory,
                goals_revision: self.goals_revision,
            },
        )
        .await
    }

    async fn check_subject(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        context: AnalysisSubject<'_>,
    ) -> Result<Option<BTreeSet<Uuid>>, sqlx::Error> {
        let zone = owner_timezone(txn, &self.configured_timezone)
            .await
            .map_err(|_| sqlx::Error::Protocol("Could not validate analysis timezone".into()))?;
        if zone != self.timezone || !onboarding_allows_model(txn, ModelCategories::DIARY).await? {
            return Ok(None);
        }
        let Some(model) = ShareDestination::new(DestinationKind::Model, &self.model).ok() else {
            return Ok(None);
        };
        if crate::conversation_memory::analysis_channel_in(txn, context)
            .await
            .map_err(|_| sqlx::Error::Protocol("Could not validate analysis context".into()))?
            .is_none()
        {
            return Ok(None);
        }
        let mut versions = BTreeSet::new();
        for read in &self.reads {
            let Some(range) = read.range() else {
                return Ok(None);
            };
            let page = match analysis_events_in(txn, context, model, range).await {
                Ok(page) => page,
                Err(DiaryReadError::Database) => {
                    return Err(sqlx::Error::Protocol(
                        "Could not validate analysis evidence".into(),
                    ));
                }
                Err(_) => return Ok(None),
            };
            if evidence_digest(&page.value).as_deref() != Some(&read.digest) {
                return Ok(None);
            }
            versions.extend(page.channel_versions);
        }
        Ok(Some(versions))
    }
}

impl DiaryReadFence {
    fn from_evidence(evidence: &Value) -> Option<Self> {
        if evidence["tool"] != "events" || evidence["result"].get("error").is_some() {
            return None;
        }
        let range = DiaryEventRange::from_analysis_arguments(evidence["arguments"].as_object()?)?;
        Some(Self {
            start: range.start.to_string(),
            end: range.end.to_string(),
            kind: range.kind.map(str::to_owned),
            digest: evidence_digest(&evidence["result"])?,
            result_hash: legacy_result_hash(&evidence["result"]),
        })
    }

    fn range(&self) -> Option<DiaryEventRange<'_>> {
        Some(DiaryEventRange {
            start: self.start.parse().ok()?,
            end: self.end.parse().ok()?,
            kind: self.kind.as_deref(),
            limit: 500,
        })
    }
}

fn evidence_digest(result: &Value) -> Option<String> {
    let bytes = serde_json::to_vec(result).ok()?;
    Some(sha256_hex(
        &encode(&bytes, Utf8Layout::Compact, bytes.len()).ok()?,
    ))
}

fn legacy_result_hash(result: &Value) -> Option<String> {
    let bytes = serde_json::to_vec(result).ok()?;
    Some(sha256_hex(
        encode_ascii(&bytes, Utf8Layout::Archive, bytes.len())
            .ok()?
            .as_bytes(),
    ))
}
