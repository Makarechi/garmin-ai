use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{Conversation, WebChatError};
use crate::{
    conversation_memory::{CONTEXT_DELIVERY_LOCK, ConversationBinding, MemoryFence},
    diary::analysis_evidence::DiaryAnalysisFence,
    personal_goals::GOAL_POLICY_LOCK,
    share_policy::{MODEL_CONSENT_LOCK, OWNER_ONBOARDING_LOCK, SharePolicyError, try_shared_lock},
};

// The Python rollback reader already rejects dependent-analysis references
// without a matching legacy record. Only Rust can validate this evidence.
pub(super) const ANALYSIS_REFERENCE: &str = "analysis-sleep-timing:native-web-diary-v1";
pub(super) const NATIVE_TIMING_REFERENCE: &str = "sleep-timing:native-web-analysis-v1";

#[derive(Deserialize, Serialize)]
pub(super) struct WebAnalysisProof {
    target_epoch: String,
    source_epochs: BTreeMap<String, String>,
    web_diary_analysis: DiaryAnalysisFence,
}

impl WebAnalysisProof {
    pub(super) fn new(memory: &MemoryFence, analysis: DiaryAnalysisFence) -> Self {
        Self {
            target_epoch: memory.target_epoch.to_string(),
            source_epochs: memory
                .source_epochs
                .iter()
                .map(|(source, epoch)| (source.to_string(), epoch.to_string()))
                .collect(),
            web_diary_analysis: analysis,
        }
    }

    pub(super) fn reference(&self) -> &'static str {
        if self.web_diary_analysis.single_sleep_timing() {
            NATIVE_TIMING_REFERENCE
        } else {
            ANALYSIS_REFERENCE
        }
    }

    fn memory(&self) -> Option<MemoryFence> {
        Some(MemoryFence {
            target_epoch: self.target_epoch.parse().ok()?,
            source_epochs: self
                .source_epochs
                .iter()
                .map(|(id, epoch)| Some((id.parse().ok()?, epoch.parse().ok()?)))
                .collect::<Option<_>>()?,
        })
    }

    pub(super) async fn store(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        id: Uuid,
    ) -> Result<(), WebChatError> {
        let proof = serde_json::to_string(self).map_err(|_| WebChatError::Database)?;
        sqlx::query("UPDATE outbox_messages SET memory_fence=$2::jsonb WHERE id=$1")
            .bind(id)
            .bind(proof)
            .execute(&mut **txn)
            .await?;
        Ok(())
    }
}

pub(super) async fn current_web_analysis(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Conversation,
    id: Uuid,
    reference: &str,
) -> Result<bool, WebChatError> {
    let point = reference == NATIVE_TIMING_REFERENCE;
    let policy = [MODEL_CONSENT_LOCK, OWNER_ONBOARDING_LOCK];
    let timing_context = [CONTEXT_DELIVERY_LOCK, GOAL_POLICY_LOCK];
    for lock in policy
        .into_iter()
        .chain(timing_context.into_iter().filter(|_| point))
    {
        match try_shared_lock(txn, lock).await {
            Ok(()) => {}
            Err(SharePolicyError::Busy) if point => return Ok(false),
            Err(_) => return Err(WebChatError::Database),
        }
    }
    let stored: Option<String> = sqlx::query_scalar(
        "SELECT memory_fence::text FROM outbox_messages WHERE id=$1 AND owner_id=$2 AND conversation_id=$3",
    )
    .bind(id)
    .bind(conversation.owner)
    .bind(conversation.id)
    .fetch_optional(&mut **txn)
    .await?
    .flatten();
    let Some(proof) = stored.and_then(|raw| serde_json::from_str::<WebAnalysisProof>(&raw).ok())
    else {
        return Ok(false);
    };
    if proof.reference() != reference {
        return Ok(false);
    }
    let Some(memory) = proof.memory() else {
        return Ok(false);
    };
    Ok(proof
        .web_diary_analysis
        .check_web(
            txn,
            ConversationBinding {
                owner_id: conversation.owner,
                conversation_id: conversation.id,
            },
            &memory,
        )
        .await?
        .is_some())
}
