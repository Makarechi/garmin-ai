use std::time::Instant;

use ga_db::{
    diary::{InterpretationContext, InterpretationContextQuery},
    share_policy::{
        DestinationKind, ModelChannelSharing, ModelConversation, ShareCategory, ShareDestination,
        ShareRequirement,
    },
    web_chat::{
        WebChatError, WebChatInterpretationContext, WebChatModelReply, WebChatModelSession,
        WebChatSnapshot, WebChatSubmission,
    },
};
use ga_domain::{
    agent_interpretation::{AgentIntent, AgentInterpretation},
    analysis_budget::AnalysisBudget,
    event_input::EventSource,
    localization::catalog_text,
};
use uuid::Uuid;

use super::{
    CurrentModelPolicy, DiaryInterpretationError, DiaryInterpretationRequest, ModelDiaryContext,
    ModelGateway, ModelRequestScope, telegram::mentions,
};
use crate::{telegram_transport::model_reply::saved_message, urgent_screen::urgent_notice};

struct WebInterpretation {
    command: AgentInterpretation,
    digest: String,
    mentioned: Vec<Uuid>,
}

impl ModelGateway<'_> {
    /// Run an owner model request under its dedicated submission lock.
    pub async fn process_web_chat<'policy>(
        &self,
        session: WebChatModelSession,
        request: WebChatSubmission<'_>,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
    ) -> Result<WebChatSnapshot, WebChatError> {
        let model = ShareDestination::new(DestinationKind::Model, self.binding().instance_id)
            .map_err(|_| WebChatError::Database)?;
        let interpreted = self
            .interpret_web_chat(&session, request.text.trim(), model, &policy)
            .await;
        let mut language = session.locale.clone();
        let reply = if let Ok(interpretation) = &interpreted {
            let command = &interpretation.command;
            if let Some(selected) = command.response_language() {
                language = selected.to_owned();
            }
            let expected = WebChatInterpretationContext {
                model,
                digest: &interpretation.digest,
                mentioned_events: &interpretation.mentioned,
            };
            match command.intent() {
                AgentIntent::Log | AgentIntent::Update | AgentIntent::Close => {
                    WebChatModelReply::Mutation { command, expected }
                }
                AgentIntent::Undo if command.confidence() < 0.85 => WebChatModelReply::Clarify {
                    question: catalog_text(&language, "agent.record_details")
                        .ok_or(WebChatError::Database)?,
                    expected,
                },
                AgentIntent::Undo => WebChatModelReply::Undo {
                    command,
                    language: &language,
                    expected,
                },
                AgentIntent::Safety => WebChatModelReply::SafetyNotice(urgent_notice(&language)),
                AgentIntent::Clarify => WebChatModelReply::Clarify {
                    question: command
                        .clarification()
                        .filter(|text| !text.trim().is_empty())
                        .or_else(|| catalog_text(&language, "agent.record_fallback"))
                        .ok_or(WebChatError::Database)?,
                    expected,
                },
                _ => WebChatModelReply::Unverified,
            }
        } else {
            WebChatModelReply::Unverified
        };
        self.database
            .finish_web_chat_model(session, request.configured_timezone, reply, |events| {
                saved_message(events, &language)
            })
            .await
    }

    async fn interpret_web_chat<'policy>(
        &self,
        session: &WebChatModelSession,
        text: &str,
        model: ShareDestination<'_>,
        policy: &(impl Fn() -> CurrentModelPolicy<'policy> + Sync),
    ) -> Result<WebInterpretation, DiaryInterpretationError> {
        let binding = session.binding();
        let mentioned = mentions(text);
        let context = self
            .database
            .interpretation_context(InterpretationContextQuery {
                binding,
                model,
                now: session.at,
                clarification_clock: session.clarification_clock(),
                mentioned_events: &mentioned,
            })
            .await
            .map_err(DiaryInterpretationError::Read)?;
        let digest = context.digest().map_err(DiaryInterpretationError::Read)?;
        let InterpretationContext {
            channel,
            events,
            recent_questions,
            pending_clarification,
            recent_analysis_question,
            memory_fence,
            ..
        } = context;
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
        let destination = ShareDestination::new(DestinationKind::Channel, &channel)
            .map_err(|_| DiaryInterpretationError::InvalidContext)?;
        let mut budget = AnalysisBudget::new(Instant::now());
        let command = self
            .interpret_with(
                DiaryInterpretationRequest {
                    now: session.at,
                    timezone: &session.timezone,
                    source: EventSource::Manual,
                    text,
                    context: ModelDiaryContext {
                        recent_analysis_question,
                        recent_events: events.entries,
                        history_truncated: events.history_truncated,
                        pending_clarification,
                        recent_questions,
                    },
                },
                &mut budget,
                policy,
                ModelRequestScope {
                    requirements: &model_requirements,
                    uses_voice_text: false,
                    channel: Some(ModelChannelSharing {
                        destination,
                        requirements: &channel_requirements,
                        conversation: Some(ModelConversation::Web {
                            binding,
                            memory: &memory_fence,
                        }),
                    }),
                    job: None,
                },
                self.sender(),
            )
            .await?;
        Ok(WebInterpretation {
            command,
            digest,
            mentioned,
        })
    }
}
