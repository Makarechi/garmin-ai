use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{Conversation, WebChatError};
use crate::{
    conversation_memory::{ConversationBinding, MemoryFence},
    diary::analysis_evidence::DiaryAnalysisFence,
    share_policy::{MODEL_CONSENT_LOCK, OWNER_ONBOARDING_LOCK, try_shared_lock},
};

// The Python rollback reader already rejects dependent-analysis references
// without a matching legacy record. Only Rust can validate this evidence.
pub(super) const ANALYSIS_REFERENCE: &str = "analysis-sleep-timing:native-web-diary-v1";

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
) -> Result<bool, WebChatError> {
    for lock in [MODEL_CONSENT_LOCK, OWNER_ONBOARDING_LOCK] {
        try_shared_lock(txn, lock)
            .await
            .map_err(|_| WebChatError::Database)?;
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
