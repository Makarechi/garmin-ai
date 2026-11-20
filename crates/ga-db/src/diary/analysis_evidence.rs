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
    health_analysis::{
        AnalysisTimelineQuery, DailyAnalysisEvidence, HealthAnalysisError, TIMELINE, TimelineError,
        analysis_timeline_in,
        generic::{GENERIC_ANALYSIS, GenericAnalysisError, GenericAnalysisEvidence},
        owner_timezone,
    },
    model_gate::onboarding::onboarding_allows_model,
    share_policy::{DestinationKind, ShareDestination},
};

#[derive(Deserialize, Serialize)]
pub(crate) struct DiaryAnalysisFence {
    model: String,
    timezone: String,
    configured_timezone: String,
    goals_revision: u64,
    reads: Vec<AnalysisReadFence>,
}

#[derive(Deserialize, Serialize)]
#[serde(untagged)]
enum AnalysisReadFence {
    Daily(DailyAnalysisEvidence),
    Generic(GenericAnalysisEvidence),
    Timeline(TimelineReadFence),
    Diary(DiaryReadFence),
}

#[derive(Deserialize, Serialize)]
pub struct TimelineReadFence {
    timeline_query: AnalysisTimelineQuery,
    timeline_result_hash: String,
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
    pub(crate) fn single_sleep_timing(&self) -> bool {
        matches!(self.reads.as_slice(), [AnalysisReadFence::Daily(read)] if read.single_sleep_timing())
    }

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
                .map(|read| {
                    if read["tool"] == GENERIC_ANALYSIS {
                        GenericAnalysisEvidence::from_read(
                            read,
                            read["generic_request_time"].as_str()?,
                        )
                        .map(AnalysisReadFence::Generic)
                    } else if read["tool"] == TIMELINE {
                        TimelineReadFence::from_read(read).map(AnalysisReadFence::Timeline)
                    } else if read["tool"]
                        .as_str()
                        .is_some_and(DailyAnalysisEvidence::is_daily_tool)
                    {
                        DailyAnalysisEvidence::from_read(read, timezone)
                            .map(AnalysisReadFence::Daily)
                    } else {
                        DiaryReadFence::from_evidence(read).map(AnalysisReadFence::Diary)
                    }
                })
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
            .map(|read| match read {
                AnalysisReadFence::Diary(read) => read.specification(),
                AnalysisReadFence::Daily(read) => read.specification(),
                AnalysisReadFence::Generic(read) => read.specification(),
                AnalysisReadFence::Timeline(read) => read.specification(),
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
        let Some(channel) = crate::conversation_memory::analysis_channel_in(txn, context)
            .await
            .map_err(|_| sqlx::Error::Protocol("Could not validate analysis context".into()))?
        else {
            return Ok(None);
        };
        let Ok(channel) = ShareDestination::new(DestinationKind::Channel, &channel) else {
            return Ok(None);
        };
        let mut versions = BTreeSet::new();
        for read in &self.reads {
            let read = match read {
                AnalysisReadFence::Diary(read) => read,
                AnalysisReadFence::Timeline(read) => {
                    match read.current(txn, context, model).await {
                        Ok(Some(current)) => versions.extend(current),
                        Err(
                            TimelineError::Database
                            | TimelineError::Health(HealthAnalysisError::Database)
                            | TimelineError::Diary(DiaryReadError::Database),
                        ) => {
                            return Err(sqlx::Error::Protocol(
                                "Could not validate timeline evidence".into(),
                            ));
                        }
                        Ok(None) | Err(_) => return Ok(None),
                    }
                    continue;
                }
                AnalysisReadFence::Generic(read) => match read
                    .current(
                        txn,
                        context.binding().owner_id,
                        model,
                        channel,
                        ModelCategories::HEALTH_AND_DIARY,
                    )
                    .await
                {
                    Ok(Some(current)) => {
                        versions.extend(current);
                        continue;
                    }
                    Err(GenericAnalysisError::Database) => {
                        return Err(sqlx::Error::Protocol(
                            "Could not validate generic analysis evidence".into(),
                        ));
                    }
                    _ => return Ok(None),
                },
                AnalysisReadFence::Daily(read) => match read.current(txn, context).await {
                    Ok(true) => continue,
                    Err(HealthAnalysisError::Database) => {
                        return Err(sqlx::Error::Protocol(
                            "Could not validate daily analysis evidence".into(),
                        ));
                    }
                    _ => return Ok(None),
                },
            };
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

impl TimelineReadFence {
    pub fn from_read(evidence: &Value) -> Option<Self> {
        if evidence["tool"] != TIMELINE || evidence["result"].get("error").is_some() {
            return None;
        }
        Some(Self {
            timeline_query: AnalysisTimelineQuery::from_arguments(
                evidence["arguments"].as_object()?,
            )?,
            timeline_result_hash: evidence_digest(&evidence["result"])?,
        })
    }

    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        context: AnalysisSubject<'_>,
        model: ShareDestination<'_>,
    ) -> Result<Option<BTreeSet<Uuid>>, TimelineError> {
        let page = analysis_timeline_in(txn, context, model, &self.timeline_query).await?;
        Ok(
            (evidence_digest(&page.value).as_deref() == Some(&self.timeline_result_hash))
                .then_some(page.tracker_versions),
        )
    }

    fn specification(&self) -> Value {
        json!({
            "tool": TIMELINE, "arguments": &self.timeline_query, "result_hash": &self.timeline_result_hash,
        })
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

    fn specification(&self) -> Value {
        let mut arguments = json!({"start":self.start,"end":self.end});
        if let Some(kind) = &self.kind {
            arguments["kind"] = json!(kind);
        }
        json!({
            "tool":"events",
            "arguments":arguments,
            "result_hash":self.result_hash.as_deref().unwrap_or(&self.digest),
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
