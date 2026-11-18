use std::time::{Duration, Instant};

use ga_db::{
    Database,
    conversation_memory::{AnalysisReadContext, ConversationBinding, ConversationReadContext},
    jobs::telegram_inbound::{
        TelegramDiaryCommitError, TelegramInboundLease, TelegramInboundTarget,
        TelegramModelAdmission,
    },
    model_gate::ModelGateError,
    outbox_enqueue::QueueIntentError,
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::{
    analysis_budget::{AnalysisBudget, AnalysisBudgetError},
    channel::InboundEnvelope,
    event_input::source_offset_datetime,
    localization::catalog_text,
    timestamp::StoredInstant,
};
use ga_model::fallback::FallbackError;
use serde_json::{Value, json};
use time::format_description::well_known::Rfc3339;
use tokio::time::sleep;

use super::{
    DiaryReplyOutcome, DiaryReplyPreferences, TelegramTransport, model_reply::text_reply,
    poller::wall_clock, publish_telegram_interpretation,
};
use crate::{
    model_gateway::{
        ActiveModelJob, AnalysisDestination, AnalysisError, AnalysisQuestion, AnalysisResult,
        ConfiguredModel, DiaryAnalysisTools, DiaryInterpretationError, GatewayError, ModelGateway,
        Sender, TelegramDiaryInterpretation,
    },
    telegram_settings::TelegramIngressSettings,
    urgent_screen::urgent_notice,
};

#[cfg(test)]
mod tests;

const LEASE: u32 = 150;
const IDLE: Duration = Duration::from_millis(250);

impl TelegramTransport {
    /// Run ordered model work separately from polling, local safety screening and reply delivery.
    pub async fn run_model_diary(
        &self,
        database: &Database,
        settings: &TelegramIngressSettings,
        configured: &ConfiguredModel<'_>,
        gateway: &ModelGateway<'_>,
        timezone: &str,
        locale: &str,
    ) -> ! {
        let worker = ModelDiaryWorker {
            database,
            configured,
            gateway,
            sender: gateway.sender(),
            timezone,
            locale,
        };
        loop {
            if let (Ok(binding), Ok(now)) =
                (settings.confirmed_binding(database).await, wall_clock())
            {
                let target = TelegramInboundTarget {
                    owner_id: binding.owner_id,
                    instance_id: binding.instance_id,
                };
                if let Ok(Some(lease)) =
                    database.claim_telegram_model_text(target, now, LEASE).await
                {
                    worker.process(&lease, now).await;
                }
            }
            sleep(IDLE).await;
        }
    }
}

struct ModelDiaryWorker<'a, 'c, S> {
    database: &'a Database,
    configured: &'a ConfiguredModel<'c>,
    gateway: &'a ModelGateway<'c>,
    sender: &'a S,
    timezone: &'a str,
    locale: &'a str,
}

impl<S: Sender> ModelDiaryWorker<'_, '_, S> {
    async fn process(&self, lease: &TelegramInboundLease, started: StoredInstant) {
        let admission = self
            .database
            .admit_telegram_model_text(lease, self.timezone, self.locale)
            .await;
        let (timezone, locale, undo_anchor) = match admission {
            Ok(TelegramModelAdmission::Ready {
                timezone,
                locale,
                undo_anchor,
            }) => (timezone, locale, undo_anchor),
            Ok(TelegramModelAdmission::Replied) => return,
            Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy)) => {
                self.finish(lease, self.locale, Disposition::Retry(IDLE))
                    .await;
                return;
            }
            Err(error) => {
                self.finish(lease, self.locale, commit_disposition(error))
                    .await;
                return;
            }
        };
        let mut budget = AnalysisBudget::new(Instant::now());
        let request = match self
            .gateway
            .interpret_telegram_diary_with(
                lease,
                &timezone,
                started,
                &mut budget,
                || self.configured.current(),
                self.sender,
            )
            .await
        {
            Ok(request) => request,
            Err(error) => {
                self.finish(lease, &locale, interpretation_disposition(error))
                    .await;
                return;
            }
        };
        let language = request.command.response_language().unwrap_or(&locale);
        let preferences = DiaryReplyPreferences {
            configured_timezone: self.timezone,
            owner_locale: &locale,
            started,
            undo_anchor,
        };
        match publish_telegram_interpretation(self.database, lease, &request, preferences).await {
            Ok(DiaryReplyOutcome::Published) => {}
            Ok(DiaryReplyOutcome::NeedsAnalysis) => {
                if let Err(disposition) = self
                    .answer(lease, &request, &mut budget, started, &timezone, language)
                    .await
                {
                    self.finish(lease, language, disposition).await;
                }
            }
            Err(error) => {
                self.finish(lease, language, commit_disposition(error))
                    .await
            }
        }
    }

    async fn answer(
        &self,
        lease: &TelegramInboundLease,
        interpretation: &TelegramDiaryInterpretation,
        budget: &mut AnalysisBudget,
        started: StoredInstant,
        timezone: &str,
        language: &str,
    ) -> Result<(), Disposition> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| Disposition::Gone)?;
        let text = envelope.text().ok_or(Disposition::Gone)?;
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        let (memory, goals) = tokio::join!(
            self.database.conversation_memory(binding, started),
            self.database.personal_goals(binding.owner_id),
        );
        let memory = memory.map_err(|_| Disposition::Text("agent.conversation_deleted"))?;
        if memory.fence != interpretation.memory {
            return Err(Disposition::Text("agent.conversation_deleted"));
        }
        let goals = goals.map_err(|_| Disposition::Text("agent.goals_changed"))?;
        let goals_document =
            serde_json::to_value(&goals).map_err(|_| Disposition::Text("agent.goals_changed"))?;
        let context = AnalysisReadContext {
            binding,
            inbound_id: lease.message_id(),
            operation_id: lease.operation_id(),
            memory: &memory.fence,
            goals_revision: goals.revision,
        };
        let effective = analysis_clock(
            envelope.occurred_at().unwrap_or(envelope.received_at()),
            timezone,
        )?;
        let channel = format!("telegram:{}", envelope.channel().instance_id());
        let tools =
            DiaryAnalysisTools::new(self.database, context, self.gateway.binding().instance_id)
                .map_err(|_| Disposition::Text("agent.data_changed"))?;
        let conversation = analysis_turns(memory.turns);
        let (id, lease_token) = lease.job_identity();
        let analysis = self
            .gateway
            .answer_for_channel(
                AnalysisQuestion {
                    now: &effective,
                    timezone,
                    text,
                    locale: language,
                    conversation: &conversation,
                    quality_context: &json!({}),
                    personal_goals: &goals_document,
                    replay_notice: None,
                    tools: &[tools.offered_tool()],
                },
                budget,
                &tools,
                || self.configured.current(),
                AnalysisDestination {
                    channel: ShareDestination::new(DestinationKind::Channel, &channel)
                        .map_err(|_| Disposition::Gone)?,
                    conversation: ConversationReadContext {
                        binding,
                        inbound_id: lease.message_id(),
                        operation_id: lease.operation_id(),
                        memory: &memory.fence,
                    },
                    tracker_versions: &interpretation.channel_versions,
                    job: ActiveModelJob { id, lease_token },
                },
                self.sender,
            )
            .await
            .map_err(analysis_disposition)?;
        match analysis {
            AnalysisResult::Safety { language } => self
                .database
                .finish_telegram_model_notice(lease, urgent_notice(&language))
                .await
                .map_err(commit_disposition),
            AnalysisResult::NoAnswer { language } => self
                .database
                .finish_telegram_model_notice(lease, template(&language, "agent.steps_exhausted"))
                .await
                .map_err(commit_disposition),
            AnalysisResult::Answer {
                language,
                narrative,
                numbers,
                timestamps,
                include_current_state_freshness,
                used_evidence,
                ..
            } => {
                if include_current_state_freshness {
                    return Err(Disposition::Text("agent.answer_unverified"));
                }
                let body = [
                    narrative.unwrap_or_default(),
                    numbers.join("\n"),
                    timestamps.join("\n"),
                ]
                .into_iter()
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join("\n\n");
                let attribution = if used_evidence.is_empty() {
                    template(&language, "agent.saved_data").to_owned()
                } else {
                    template(&language, "agent.saved_sources")
                        .replace("{sources}", template(&language, "agent.source.diary"))
                };
                let intent = text_reply(&envelope, &(body + &attribution))
                    .ok_or(Disposition::Text("agent.answer_unverified"))?;
                self.database
                    .queue_telegram_diary_analysis(
                        lease,
                        interpretation.publication_context(self.timezone),
                        goals.revision,
                        &used_evidence,
                        intent,
                    )
                    .await
                    .map_err(commit_disposition)?;
                Ok(())
            }
        }
    }

    async fn finish(&self, lease: &TelegramInboundLease, language: &str, disposition: Disposition) {
        match disposition {
            Disposition::Gone => {}
            Disposition::Text(key) => {
                let _ = self
                    .database
                    .finish_telegram_model_notice(lease, template(language, key))
                    .await;
            }
            Disposition::Retry(delay) => {
                if let Ok(now) = wall_clock() {
                    let delay = i64::try_from(delay.as_micros()).unwrap_or(i64::MAX);
                    if let Ok(retry) = StoredInstant::from_unix_microseconds(
                        now.unix_microseconds().saturating_add(delay),
                    ) {
                        let _ = self
                            .database
                            .defer_telegram_model_text(lease, now, retry)
                            .await;
                    }
                }
            }
        }
    }
}

fn analysis_clock(message: time::OffsetDateTime, timezone: &str) -> Result<String, Disposition> {
    let now = StoredInstant::try_from(message)
        .ok()
        .and_then(|at| source_offset_datetime(at, timezone))
        .and_then(|at| at.format(&Rfc3339).ok())
        .ok_or(Disposition::Text("agent.data_changed"))?;
    Ok(now.replace('Z', "+00:00"))
}

fn analysis_turns(turns: Vec<Value>) -> Value {
    Value::Array(turns.into_iter().filter(|turn| turn["question"].is_string()).map(|turn| {
        json!({"question":turn["question"],"asked_at":turn["asked_at"],"answer":turn["answer"]})
    }).collect())
}

enum Disposition {
    Gone,
    Retry(Duration),
    Text(&'static str),
}

fn template(language: &str, key: &str) -> &'static str {
    catalog_text(language, key).unwrap_or_default()
}

fn commit_disposition(error: TelegramDiaryCommitError) -> Disposition {
    match error {
        TelegramDiaryCommitError::Job(_) | TelegramDiaryCommitError::BindingChanged => {
            Disposition::Gone
        }
        TelegramDiaryCommitError::Outbox(QueueIntentError::Busy) => Disposition::Retry(IDLE),
        TelegramDiaryCommitError::ModelContextChanged => Disposition::Text("agent.data_changed"),
        _ => Disposition::Text("agent.record_details"),
    }
}

fn interpretation_disposition(error: DiaryInterpretationError) -> Disposition {
    match error {
        DiaryInterpretationError::Model(error) => model_disposition(error),
        DiaryInterpretationError::Budget(error) => budget_disposition(error),
        DiaryInterpretationError::RequiresSafetyScreen => Disposition::Text("agent.oversized"),
        DiaryInterpretationError::ExpiredSelection => Disposition::Text("agent.selection_expired"),
        DiaryInterpretationError::HistoryTooLarge => Disposition::Text("agent.history_too_large"),
        DiaryInterpretationError::UnofferedTarget => Disposition::Text("agent.record_missing"),
        DiaryInterpretationError::InvalidTelegramMessage => Disposition::Gone,
        _ => Disposition::Text("agent.record_details"),
    }
}

fn analysis_disposition(error: AnalysisError) -> Disposition {
    match error {
        AnalysisError::Model(error) => model_disposition(error),
        AnalysisError::Budget(error) => budget_disposition(error),
        AnalysisError::EvidenceChanged => Disposition::Text("agent.data_changed"),
        _ => Disposition::Text("agent.answer_unverified"),
    }
}

fn model_disposition(error: GatewayError) -> Disposition {
    match error {
        GatewayError::Attempt(FallbackError::Cooldown { retry_after, .. }) => {
            Disposition::Retry(retry_after)
        }
        GatewayError::State(ModelGateError::Busy) => Disposition::Retry(IDLE),
        GatewayError::State(ModelGateError::LeaseLost) => Disposition::Gone,
        GatewayError::Attempt(FallbackError::Deadline) => Disposition::Text("agent.timeout"),
        GatewayError::Consent(_) | GatewayError::Attempt(FallbackError::ConsentRequired) => {
            Disposition::Text("agent.data_changed")
        }
        _ => Disposition::Text("agent.answer_unverified"),
    }
}

fn budget_disposition(error: AnalysisBudgetError) -> Disposition {
    Disposition::Text(match error {
        AnalysisBudgetError::Limit => "agent.budget",
        AnalysisBudgetError::Timeout => "agent.timeout",
    })
}
