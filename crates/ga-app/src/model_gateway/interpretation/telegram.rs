use std::collections::BTreeSet;

use ga_db::{
    conversation_memory::{ConversationBinding, ConversationReadContext, MemoryFence},
    diary::{ClarificationClock, InterpretationContext, InterpretationContextQuery},
    jobs::telegram_inbound::{TelegramInboundLease, TelegramModelContext},
    share_policy::{
        DestinationKind, ModelChannelSharing, ShareCategory, ShareDestination, ShareRequirement,
    },
};
use ga_domain::{
    agent_interpretation::{AgentIntent, AgentInterpretation},
    analysis_budget::AnalysisBudget,
    channel::{InboundEnvelope, InboundKind},
    event_input::source_offset_datetime,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::model_gateway::sleep_review_period;

use super::{
    ActiveModelJob, CurrentModelPolicy, DiaryInterpretationError, DiaryInterpretationRequest,
    EVENT_LIMIT, ModelDiaryContext, ModelGateway, ModelRequestScope, OWNER_TEXT_LIMIT, Sender,
};

/// The classification and the context generation on which it was based.
pub struct TelegramDiaryInterpretation {
    pub command: AgentInterpretation,
    pub memory: MemoryFence,
    pub channel_versions: BTreeSet<Uuid>,
    pub model_versions: BTreeSet<Uuid>,
    instance: String,
    timezone: String,
    mentioned: Vec<Uuid>,
    digest: String,
}

impl TelegramDiaryInterpretation {
    #[must_use]
    pub fn publication_context<'a>(
        &'a self,
        configured_timezone: &'a str,
    ) -> TelegramModelContext<'a> {
        TelegramModelContext {
            timezone: &self.timezone,
            configured_timezone,
            model_instance: &self.instance,
            mentioned_events: &self.mentioned,
            digest: &self.digest,
        }
    }
}

impl ModelGateway<'_> {
    /// Prepare a model request from the native Telegram inbox rather than from
    /// an independently supplied message or unverified channel.
    pub async fn interpret_telegram_diary_message<'policy>(
        &self,
        lease: &TelegramInboundLease,
        timezone: &str,
        processing_started: StoredInstant,
        budget: &mut AnalysisBudget,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
    ) -> Result<TelegramDiaryInterpretation, DiaryInterpretationError> {
        self.interpret_telegram_diary_with(
            lease,
            timezone,
            processing_started,
            budget,
            policy,
            &self.transport,
        )
        .await
    }

    pub(crate) async fn interpret_telegram_diary_with<'policy>(
        &self,
        lease: &TelegramInboundLease,
        timezone: &str,
        processing_started: StoredInstant,
        budget: &mut AnalysisBudget,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
        sender: &impl Sender,
    ) -> Result<TelegramDiaryInterpretation, DiaryInterpretationError> {
        let text = TelegramText::from_lease(lease, timezone, processing_started)?;
        self.interpret_telegram_with(lease, text, budget, &policy, sender)
            .await
    }

    async fn interpret_telegram_with<'policy, S: Sender>(
        &self,
        lease: &TelegramInboundLease,
        input: TelegramText<'_>,
        budget: &mut AnalysisBudget,
        policy: &(impl Fn() -> CurrentModelPolicy<'policy> + Sync),
        sender: &S,
    ) -> Result<TelegramDiaryInterpretation, DiaryInterpretationError> {
        let TelegramText {
            envelope,
            timezone,
            clock,
        } = input;
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        let model = ShareDestination::new(DestinationKind::Model, self.binding().instance_id)
            .map_err(|_| DiaryInterpretationError::InvalidContext)?;
        let text = envelope
            .text()
            .ok_or(DiaryInterpretationError::InvalidTelegramMessage)?;
        if text.chars().take(OWNER_TEXT_LIMIT + 1).count() > OWNER_TEXT_LIMIT {
            return Err(DiaryInterpretationError::RequiresSafetyScreen);
        }
        let mentioned = mentions(text);
        let context = self
            .database
            .interpretation_context(InterpretationContextQuery {
                binding,
                model,
                now: clock.message_at,
                clarification_clock: clock,
                mentioned_events: &mentioned,
            })
            .await
            .map_err(DiaryInterpretationError::Read)?;
        let digest = context.digest().map_err(DiaryInterpretationError::Read)?;
        if let Some(at) = source_offset_datetime(clock.message_at, timezone)
            && sleep_review_period(text, at.date()).is_some()
        {
            let language = if text.chars().any(|letter| ('А'..='я').contains(&letter)) {
                "ru"
            } else {
                "en"
            };
            let command = AgentInterpretation::from_value(&json!({
                "intent":"question","confidence":1.0,"response_language":language
            }))
            .map_err(DiaryInterpretationError::Response)?;
            return Ok(TelegramDiaryInterpretation {
                command,
                memory: context.memory_fence,
                channel_versions: BTreeSet::new(),
                model_versions: BTreeSet::new(),
                instance: self.binding().instance_id.to_owned(),
                timezone: timezone.to_owned(),
                mentioned,
                digest,
            });
        }
        let InterpretationContext {
            channel,
            mut events,
            recent_questions,
            mut pending_clarification,
            selection_unavailable,
            recent_analysis_question,
            memory_fence,
        } = context;
        let mut expired_selection = mentioned.is_empty() && selection_unavailable;
        if mentioned.is_empty()
            && let Some(pending) = pending_clarification.as_ref()
        {
            match pending_selection(&events.entries, pending, clock.message_at) {
                PendingSelection::Selected(selected) => {
                    events.entries = selected;
                    events.history_truncated = false;
                }
                PendingSelection::Any => {}
                PendingSelection::Expired => {
                    expired_selection = true;
                    pending_clarification = None;
                }
            }
        }
        let destination = ShareDestination::new(DestinationKind::Channel, &channel)
            .map_err(|_| DiaryInterpretationError::InvalidContext)?;
        let model_requirements: Vec<_> = events
            .tracker_versions
            .iter()
            .map(|&version_id| ShareRequirement {
                version_id,
                categories: &[ShareCategory::Facts],
            })
            .collect();
        let channel_requirements: Vec<_> = events
            .channel_versions
            .iter()
            .map(|&version_id| ShareRequirement {
                version_id,
                categories: &[ShareCategory::Schema, ShareCategory::Facts],
            })
            .collect();
        let context = ConversationReadContext {
            binding,
            inbound_id: lease.message_id(),
            operation_id: lease.operation_id(),
            memory: &memory_fence,
        };
        let (id, lease_token) = lease.job_identity();
        let command = self
            .interpret_with(
                DiaryInterpretationRequest {
                    now: clock.message_at,
                    timezone,
                    source: lease.diary_source(),
                    text,
                    context: ModelDiaryContext {
                        recent_analysis_question,
                        recent_events: events.entries,
                        history_truncated: events.history_truncated,
                        pending_clarification,
                        recent_questions,
                    },
                },
                budget,
                policy,
                ModelRequestScope {
                    uses_voice_text: lease.has_voice_text(),
                    requirements: &model_requirements,
                    channel: Some(ModelChannelSharing {
                        destination,
                        requirements: &channel_requirements,
                        conversation: Some(ga_db::share_policy::ModelConversation::Inbound(
                            context,
                        )),
                    }),
                    job: Some(ActiveModelJob { id, lease_token }),
                },
                sender,
            )
            .await?;
        if expired_selection && command.intent() != AgentIntent::Safety {
            return Err(DiaryInterpretationError::ExpiredSelection);
        }
        Ok(TelegramDiaryInterpretation {
            command,
            memory: memory_fence,
            channel_versions: events.channel_versions,
            model_versions: events.tracker_versions,
            instance: self.binding().instance_id.to_owned(),
            timezone: timezone.to_owned(),
            mentioned,
            digest,
        })
    }
}

struct TelegramText<'a> {
    envelope: InboundEnvelope,
    timezone: &'a str,
    clock: ClarificationClock,
}

impl<'a> TelegramText<'a> {
    fn from_lease(
        lease: &TelegramInboundLease,
        timezone: &'a str,
        now: StoredInstant,
    ) -> Result<Self, DiaryInterpretationError> {
        let envelope = lease
            .processing_envelope()
            .map_err(|_| DiaryInterpretationError::InvalidTelegramMessage)?;
        if envelope.channel().channel() != "telegram"
            || envelope.message_id() != lease.message_id()
            || !(matches!(envelope.kind(), InboundKind::Text | InboundKind::Edit)
                || lease.has_voice_text())
            || envelope.text().is_none_or(|text| text.trim().is_empty())
        {
            return Err(DiaryInterpretationError::InvalidTelegramMessage);
        }
        let message_at =
            StoredInstant::try_from(envelope.occurred_at().unwrap_or(envelope.received_at()))
                .map_err(|_| DiaryInterpretationError::InvalidTelegramMessage)?;
        Ok(Self {
            envelope,
            timezone,
            clock: ClarificationClock {
                message_at,
                conversation_now: now,
                use_message_time: false,
            },
        })
    }
}

#[derive(Debug, PartialEq)]
enum PendingSelection {
    Any,
    Selected(Vec<Value>),
    Expired,
}

fn pending_selection(entries: &[Value], pending: &Value, now: StoredInstant) -> PendingSelection {
    if !matches!(pending["action"].as_str(), Some("update" | "close")) {
        return PendingSelection::Any;
    }
    let Some(ids) = pending["event_ids"].as_array() else {
        return PendingSelection::Any;
    };
    let selected: Vec<_> = entries
        .iter()
        .filter(|event| ids.contains(&event["id"]))
        .collect();
    if pending["explicit_selector"] == true
        && ids.len() == 1
        && (selected
            .first()
            .is_none_or(|event| event["revision"] != pending["selection_revision"])
            || StoredInstant::from_legacy_input(&pending["selection_expires_at"])
                .is_none_or(|expires| expires <= now))
    {
        return PendingSelection::Expired;
    }
    let complete = pending["targets_complete"]
        .as_bool()
        .unwrap_or(ids.len() < EVENT_LIMIT);
    if complete && (1..=EVENT_LIMIT).contains(&ids.len()) && ids.len() == selected.len() {
        PendingSelection::Selected(selected.into_iter().cloned().collect())
    } else {
        PendingSelection::Any
    }
}

pub(super) fn mentions(text: &str) -> Vec<Uuid> {
    let bytes = text.as_bytes();
    let mut ids = Vec::new();
    let mut seen = BTreeSet::new();
    for (start, candidate) in bytes.windows(36).enumerate() {
        if !candidate[0].is_ascii_hexdigit()
            || candidate[8] != b'-'
            || (start > 0 && bytes[start - 1].is_ascii_hexdigit())
            || bytes.get(start + 36).is_some_and(u8::is_ascii_hexdigit)
        {
            continue;
        }
        if let Ok(id) = Uuid::try_parse_ascii(candidate)
            && seen.insert(id)
        {
            ids.push(id);
            // The database returns the same over-limit condition without looking up these records.
            if ids.len() > EVENT_LIMIT {
                break;
            }
        }
    }
    ids
}

#[cfg(test)]
mod tests;
