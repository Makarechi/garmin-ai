use std::collections::BTreeSet;

use ga_domain::{
    channel::{InboundEnvelope, OutboundIntent},
    model_consent::ModelCategories,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{TelegramDiaryCommitError, TelegramInboundLease as Lease};
use crate::{
    conversation_memory::{ConversationBinding, MemoryFence},
    diary::{
        ClarificationClock, DiaryReadError, InterpretationContextQuery, interpretation_context_in,
        lock_clarification,
    },
    health_analysis::owner_timezone,
    model_gate::onboarding::onboarding_allows_model,
    outbox_enqueue::{QueueIntent, QueueIntentError, QueuedIntent, queue_generated_outbox_intent},
    share_policy::{
        CHANNEL_CONSENT_LOCK, DestinationKind, MODEL_CONSENT_LOCK, ShareCategory, ShareDestination,
        SharePolicyError, ShareRequirement, try_shared_lock,
    },
};

#[derive(Clone, Copy)]
pub struct TelegramModelContext<'a> {
    pub timezone: &'a str,
    pub configured_timezone: &'a str,
    pub model_instance: &'a str,
    pub mentioned_events: &'a [Uuid],
    pub digest: &'a str,
}

pub(super) struct CurrentTelegramModelContext {
    pub(super) memory: MemoryFence,
    pub(super) offered: Vec<Value>,
    pub(super) questions: Vec<Value>,
    pub(super) versions: BTreeSet<Uuid>,
    pub(super) pending_clarification: Option<Value>,
    pub(super) selection_unavailable: bool,
}

impl CurrentTelegramModelContext {
    pub(super) fn active_clarification(&self) -> Option<Value> {
        self.pending_clarification.clone()
    }
}

pub(super) async fn verify(
    txn: &mut Transaction<'_, Postgres>,
    lease: &Lease,
    envelope: &InboundEnvelope,
    expected: TelegramModelContext<'_>,
) -> Result<CurrentTelegramModelContext, TelegramDiaryCommitError> {
    use TelegramDiaryCommitError::{Database, ModelContextChanged};
    lock_clarification(txn)
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    for lock in [CHANNEL_CONSENT_LOCK, MODEL_CONSENT_LOCK] {
        try_shared_lock(txn, lock).await.map_err(|error| {
            if matches!(error, SharePolicyError::Busy) {
                TelegramDiaryCommitError::Outbox(QueueIntentError::Busy)
            } else {
                Database
            }
        })?;
    }
    let timezone = owner_timezone(txn, expected.configured_timezone)
        .await
        .map_err(|_| Database)?;
    if timezone != expected.timezone
        || !onboarding_allows_model(txn, ModelCategories::HEALTH_AND_DIARY).await?
    {
        return Err(ModelContextChanged);
    }
    let model = ShareDestination::new(DestinationKind::Model, expected.model_instance)
        .map_err(|_| ModelContextChanged)?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let at = StoredInstant::try_from(at).map_err(|_| Database)?;
    let binding = ConversationBinding {
        owner_id: envelope.owner_id(),
        conversation_id: envelope.conversation_id(),
    };
    let local_prompt = super::history::prompt::inspect(txn, binding, lease, envelope, at).await?;
    if local_prompt.guided_form || local_prompt.draft.is_some() || local_prompt.notice.is_some() {
        return Err(ModelContextChanged);
    }
    let message_at =
        StoredInstant::try_from(envelope.occurred_at().unwrap_or(envelope.received_at()))
            .map_err(|_| ModelContextChanged)?;
    let current = interpretation_context_in(
        txn,
        InterpretationContextQuery {
            binding,
            model,
            now: message_at,
            clarification_clock: ClarificationClock {
                message_at,
                conversation_now: at,
                use_message_time: false,
            },
            mentioned_events: expected.mentioned_events,
        },
    )
    .await
    .map_err(|error| match error {
        DiaryReadError::ContextChanged | DiaryReadError::ModelConsent | DiaryReadError::Owner => {
            ModelContextChanged
        }
        _ => Database,
    })?;
    if current.digest().map_err(|_| Database)? != expected.digest {
        return Err(ModelContextChanged);
    }
    Ok(CurrentTelegramModelContext {
        memory: current.memory_fence,
        offered: current.events.entries,
        questions: current.recent_questions,
        versions: current.events.channel_versions,
        pending_clarification: current.pending_clarification,
        selection_unavailable: current.selection_unavailable,
    })
}

pub(super) async fn queue(
    txn: &mut Transaction<'_, Postgres>,
    lease: &Lease,
    context: CurrentTelegramModelContext,
    intent: OutboundIntent,
) -> Result<QueuedIntent, TelegramDiaryCommitError> {
    let mut raw = intent.document().clone();
    let mut recorded: BTreeSet<String> = raw["evidence_refs"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect();
    let ids: Vec<_> = context.versions.iter().copied().collect();
    let definitions: Vec<(Uuid, Uuid)> =
        sqlx::query_as("SELECT id,definition_id FROM event_definition_versions WHERE id=ANY($1)")
            .bind(&ids)
            .fetch_all(&mut **txn)
            .await?;
    if definitions.len() != ids.len() {
        return Err(TelegramDiaryCommitError::ModelContextChanged);
    }
    for (version, definition) in definitions {
        recorded.insert(format!("definition:{definition}"));
        recorded.insert(format!("definition-version:{version}"));
    }
    raw["evidence_refs"] = json!(recorded);
    let intent = OutboundIntent::normalize(raw).map_err(|_| TelegramDiaryCommitError::Reply)?;
    let requirements: Vec<_> = context
        .versions
        .iter()
        .map(|&version_id| ShareRequirement {
            version_id,
            categories: &[ShareCategory::Schema, ShareCategory::Facts],
        })
        .collect();
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let now = StoredInstant::try_from(now).map_err(|_| TelegramDiaryCommitError::Database)?;
    queue_generated_outbox_intent(
        txn,
        QueueIntent {
            intent,
            operation_id: lease.operation_id(),
            inbound_id: Some(lease.message_id()),
            dedup_key: None,
            requirements: &requirements,
            now,
        },
        &context.memory,
    )
    .await
    .map_err(TelegramDiaryCommitError::Outbox)?
    .ok_or(TelegramDiaryCommitError::ModelContextChanged)
}
