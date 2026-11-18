use std::{collections::BTreeSet, time::Duration};

use ga_domain::{
    channel::OutboundIntent,
    legacy_json::{Utf8Layout, encode},
    model_consent::ModelCategories,
    sha256_hex,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{
    TelegramDiaryCommitError, TelegramInboundLease, TelegramModelContext,
    diary::{complete_reply, text_envelope},
    model_publication::{queue, verify},
};
use crate::{
    Database,
    conversation_memory::{AnalysisReadContext, ConversationBinding, MemoryFence},
    diary::{DiaryEventRange, DiaryReadError, analysis_events_in},
    health_analysis::owner_timezone,
    model_gate::onboarding::onboarding_allows_model,
    outbox_enqueue::QueuedIntent,
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
}

impl Database {
    /// Revalidate cited diary reads and attach their exact dependencies to the durable answer.
    pub async fn queue_telegram_diary_analysis(
        &self,
        lease: &TelegramInboundLease,
        expected: TelegramModelContext<'_>,
        goals_revision: u64,
        evidence: &[Value],
        intent: OutboundIntent,
    ) -> Result<QueuedIntent, TelegramDiaryCommitError> {
        let envelope = text_envelope(lease)?;
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        if intent.owner_id() != binding.owner_id
            || intent.conversation_id() != binding.conversation_id
            || intent.channel().channel() != "telegram"
            || intent.channel().instance_id() != envelope.channel().instance_id()
        {
            return Err(TelegramDiaryCommitError::Reply);
        }
        let fence = DiaryAnalysisFence {
            model: expected.model_instance.to_owned(),
            timezone: expected.timezone.to_owned(),
            configured_timezone: expected.configured_timezone.to_owned(),
            goals_revision,
            reads: evidence
                .iter()
                .map(DiaryReadFence::from_evidence)
                .collect::<Option<_>>()
                .ok_or(TelegramDiaryCommitError::Reply)?,
        };
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            let mut context = verify(&mut txn, lease, &envelope, expected, ModelCategories::DIARY).await?;
            let current = fence.check(&mut txn, binding, lease.message_id(), lease.operation_id(), &context.memory)
                .await?.ok_or(TelegramDiaryCommitError::ModelContextChanged)?;
            context.versions.extend(current);
            let queued = queue(&mut txn, lease, context, intent).await?;
            let proof = serde_json::to_string(&fence).map_err(|_| TelegramDiaryCommitError::Reply)?;
            sqlx::query("UPDATE outbox_messages SET memory_fence=memory_fence||jsonb_build_object('telegram_diary_analysis',$2::jsonb)
                         WHERE id=$1").bind(queued.outbox_id).bind(proof).execute(&mut *txn).await?;
            complete_reply(&mut txn, lease).await?;
            txn.commit().await?;
            Ok(queued)
        }).await.map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

impl DiaryAnalysisFence {
    pub(crate) async fn check(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        binding: ConversationBinding,
        inbound_id: Uuid,
        operation_id: Uuid,
        memory: &MemoryFence,
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
        let context = AnalysisReadContext {
            binding,
            inbound_id,
            operation_id,
            memory,
            goals_revision: self.goals_revision,
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
