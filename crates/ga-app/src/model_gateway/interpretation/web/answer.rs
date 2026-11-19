use ga_db::{
    conversation_memory::AnalysisSubject,
    share_policy::{DestinationKind, ModelConversation, ShareDestination},
    web_chat::{WebChatInterpretationContext, WebChatModelReply, WebChatModelSession},
};
use ga_domain::{
    analysis_budget::AnalysisBudget, event_input::source_offset_datetime,
    localization::catalog_text,
};
use serde_json::{Value, json};
use time::format_description::well_known::Rfc3339;

use super::{CurrentModelPolicy, ModelGateway, WebInterpretation, urgent_notice};
use crate::model_gateway::{
    AnalysisDestination, AnalysisQuestion, AnalysisResult, DiaryAnalysisTools,
    analysis_conversation, format_diary_answer,
};

pub(super) enum WebAnswer<'a> {
    Notice(&'static str),
    Grounded {
        text: String,
        evidence: Vec<Value>,
        goals_revision: u64,
        interpretation: &'a WebInterpretation,
    },
}

impl WebAnswer<'_> {
    pub(super) fn reply<'a>(
        &'a self,
        expected: WebChatInterpretationContext<'a>,
    ) -> WebChatModelReply<'a> {
        match self {
            Self::Notice(notice) => WebChatModelReply::FixedNotice(notice),
            Self::Grounded {
                text,
                evidence,
                goals_revision,
                interpretation,
            } => WebChatModelReply::Analysis {
                answer: text,
                memory: &interpretation.memory,
                goals_revision: *goals_revision,
                evidence,
                expected,
            },
        }
    }
}

impl ModelGateway<'_> {
    pub(super) async fn answer_web_chat<'a, 'policy>(
        &self,
        session: &WebChatModelSession,
        text: &str,
        language: &str,
        interpretation: &'a WebInterpretation,
        budget: &mut AnalysisBudget,
        policy: &(impl Fn() -> CurrentModelPolicy<'policy> + Sync),
    ) -> Option<WebAnswer<'a>> {
        let binding = session.binding();
        let (memory, goals) = tokio::join!(
            self.database.conversation_memory(binding, session.at),
            self.database.personal_goals(binding.owner_id),
        );
        let memory = memory.ok()?;
        if memory.fence != interpretation.memory {
            return None;
        }
        let goals = goals.ok()?;
        let now = source_offset_datetime(session.at, &session.timezone)?
            .format(&Rfc3339)
            .ok()?
            .replace('Z', "+00:00");
        let channel = ShareDestination::new(DestinationKind::Channel, "web:local").ok()?;
        let tools = DiaryAnalysisTools::new(
            &self.database,
            AnalysisSubject::Web {
                binding,
                memory: &memory.fence,
                goals_revision: goals.revision,
            },
            self.binding().instance_id,
        )
        .ok()?;
        let answer = self
            .answer_for_channel(
                AnalysisQuestion {
                    now: &now,
                    timezone: &session.timezone,
                    text,
                    locale: language,
                    conversation: &analysis_conversation(memory.turns),
                    quality_context: &json!({}),
                    personal_goals: &serde_json::to_value(&goals).ok()?,
                    replay_notice: None,
                    tools: &[tools.offered_tool()],
                },
                budget,
                &tools,
                policy,
                AnalysisDestination {
                    channel,
                    conversation: ModelConversation::Web {
                        binding,
                        memory: &memory.fence,
                    },
                    tracker_versions: &interpretation.channel_versions,
                    job: None,
                    uses_voice_text: false,
                },
                self.sender(),
            )
            .await
            .ok()?;
        match answer {
            AnalysisResult::Safety { language } => {
                Some(WebAnswer::Notice(urgent_notice(&language)))
            }
            AnalysisResult::NoAnswer { language } => Some(WebAnswer::Notice(catalog_text(
                &language,
                "agent.steps_exhausted",
            )?)),
            AnalysisResult::Answer {
                language,
                narrative,
                numbers,
                timestamps,
                include_current_state_freshness: false,
                used_evidence,
                ..
            } => Some(WebAnswer::Grounded {
                text: format_diary_answer(
                    &language,
                    narrative,
                    numbers,
                    timestamps,
                    !used_evidence.is_empty(),
                ),
                evidence: used_evidence,
                goals_revision: goals.revision,
                interpretation,
            }),
            _ => None,
        }
    }
}
