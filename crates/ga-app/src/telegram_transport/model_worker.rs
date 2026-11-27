use std::time::{Duration, Instant};

use ga_db::{
    Database, WorkTopic,
    conversation_memory::{AnalysisReadContext, ConversationBinding, MemoryError},
    jobs::telegram_inbound::{
        TelegramAnalysisPublication, TelegramDiaryCommitError, TelegramInboundLease,
        TelegramInboundTarget, TelegramModelAdmission,
    },
    model_gate::ModelGateError,
    outbox_enqueue::QueueIntentError,
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::{
    analysis_budget::{AnalysisBudget, AnalysisBudgetError},
    event_input::source_offset_datetime,
    localization::catalog_text,
    model_consent::ModelProvider,
    model_response::{ModelResponseError, StructuredProvider},
    timestamp::StoredInstant,
};
use ga_model::{
    ModelFailureDetail, ModelTransportError, ModelUnavailability, fallback::FallbackError,
};
use time::format_description::well_known::Rfc3339;
use tokio::time::sleep;

use super::{
    DiaryReplyOutcome, DiaryReplyPreferences, TelegramTransport, model_reply::text_reply,
    poller::wall_clock, publish_telegram_interpretation, worker::IDLE_RECHECK,
};
use crate::{
    model_gateway::{
        ActiveModelJob, AnalysisDestination, AnalysisError, AnalysisQuestion, AnalysisReadTools,
        AnalysisResult, ConfiguredModel, DiaryAnalysisTools, DiaryInterpretationError,
        GatewayError, ModelGateway, ReplySafetyIntent, Sender, TelegramDiaryInterpretation,
        analysis_conversation, format_analysis_answer, sleep_review_period,
    },
    telegram_settings::TelegramIngressSettings,
    urgent_screen::urgent_notice,
};

#[cfg(test)]
mod tests;
mod voice;

const LEASE: u32 = 150;
const IDLE: Duration = Duration::from_millis(250);
const VOICE_REPLY_RESERVE: Duration = Duration::from_secs(4);

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
        let mut work = database.work_subscription(WorkTopic::TelegramInbound);
        loop {
            let mut idle = false;
            if let (Ok(binding), Ok(now)) =
                (settings.confirmed_binding(database).await, wall_clock())
            {
                let target = TelegramInboundTarget {
                    owner_id: binding.owner_id,
                    instance_id: binding.instance_id,
                };
                match database.claim_telegram_model_text(target, now, LEASE).await {
                    Ok(Some(lease)) => {
                        worker.process(&lease, now).await;
                        continue;
                    }
                    Ok(None) => match database.claim_telegram_voice(target, now, LEASE).await {
                        Ok(Some(lease)) => {
                            worker.process_voice(self, &lease, now).await;
                            continue;
                        }
                        Ok(None) => idle = true,
                        Err(_) => {}
                    },
                    Err(_) => {}
                }
            }
            if idle {
                work.wait(IDLE_RECHECK).await;
            } else {
                sleep(IDLE).await;
            }
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
        if lease.has_voice_text()
            && self
                .gateway
                .authorize_transcription(self.configured.current(), started)
                .is_err()
        {
            self.finish(lease, self.locale, Disposition::VoiceUnavailable)
                .await;
            return;
        }
        let admission = self
            .database
            .admit_telegram_model_text(lease, self.timezone, self.locale)
            .await;
        let (timezone, locale, undo_anchor, legacy_tracker) = match admission {
            Ok(TelegramModelAdmission::Ready {
                timezone,
                locale,
                undo_anchor,
                legacy_tracker,
            }) => (timezone, locale, undo_anchor, legacy_tracker),
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
        if legacy_tracker {
            if let Err(error) = self
                .gateway
                .process_telegram_tracker_text(
                    lease,
                    &timezone,
                    &locale,
                    &|| self.configured.current(),
                    self.sender,
                )
                .await
            {
                self.finish(lease, &locale, commit_disposition(error)).await;
            }
            return;
        }
        let Ok(envelope) = lease.processing_envelope() else {
            self.finish(lease, &locale, Disposition::Gone).await;
            return;
        };
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        // Recover confirmed replies before interpretation captures the conversation digest.
        if let Err(error) = self.database.conversation_memory(binding, started).await {
            let disposition = match error {
                MemoryError::Busy | MemoryError::Storage(_) | MemoryError::Database => {
                    Disposition::Retry(IDLE)
                }
                _ => Disposition::Text("agent.conversation_deleted"),
            };
            self.finish(lease, &locale, disposition).await;
            return;
        }
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
            Err(DiaryInterpretationError::RequiresSafetyScreen) if lease.has_voice_text() => {
                let result = self.handle_long_voice_text(lease, &locale, &budget).await;
                if let Err(error) = result {
                    self.finish(lease, &locale, commit_disposition(error)).await;
                }
                return;
            }
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
                    .await;
            }
        }
    }

    async fn handle_long_voice_text(
        &self,
        lease: &TelegramInboundLease,
        locale: &str,
        budget: &AnalysisBudget,
    ) -> Result<(), TelegramDiaryCommitError> {
        let lease_remaining =
            Duration::try_from(lease.expires_at() - time::OffsetDateTime::now_utc())
                .unwrap_or_default()
                .saturating_sub(VOICE_REPLY_RESERVE);
        let deadline = tokio::time::Instant::now()
            + lease_remaining.min(budget.remaining(Instant::now()).unwrap_or_default());
        let safety = self
            .gateway
            .screen_voice_reply_safety_with(
                lease,
                locale,
                &|| self.configured.current(),
                deadline,
                self.sender,
            )
            .await?;
        let message = if safety.intent == ReplySafetyIntent::Safety && !safety.oversized_notice {
            urgent_notice(&safety.language)
        } else {
            template(&safety.language, "agent.oversized")
        };
        self.database
            .finish_telegram_model_notice(lease, message)
            .await
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
        let envelope = lease.processing_envelope().map_err(|_| Disposition::Gone)?;
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
        let question_at = envelope.occurred_at().unwrap_or(envelope.received_at());
        let effective = analysis_clock(question_at, timezone)?;
        let question_clock = StoredInstant::try_from(question_at)
            .map_err(|_| Disposition::Text("agent.data_changed"))?;
        let channel = format!("telegram:{}", envelope.channel().instance_id());
        let tools =
            DiaryAnalysisTools::new(self.database, context, self.gateway.binding().instance_id)
                .map_err(|_| Disposition::Text("agent.data_changed"))?
                .with_daily_access(
                    self.gateway.binding(),
                    self.configured.current(),
                    started,
                    lease.has_voice_text(),
                    self.timezone,
                    question_clock,
                )
                .await
                .with_telegram_sleep_review(
                    source_offset_datetime(question_clock, timezone)
                        .and_then(|at| sleep_review_period(text, at.date())),
                );
        let conversation = analysis_conversation(memory.turns);
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
                    personal_goals: &goals_document,
                    replay_notice: None,
                    tools: &tools.offered_tools(),
                },
                budget,
                &tools,
                || self.configured.current(),
                AnalysisDestination {
                    channel: ShareDestination::new(DestinationKind::Channel, &channel)
                        .map_err(|_| Disposition::Gone)?,
                    subject: context.into(),
                    tracker_versions: &interpretation.channel_versions,
                    job: Some(ActiveModelJob { id, lease_token }),
                    uses_voice_text: lease.has_voice_text(),
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
            AnalysisResult::Replay { language } => self
                .database
                .finish_telegram_model_notice(lease, template(&language, "agent.replay"))
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
                freshness,
                used_evidence,
                schema_versions,
                ..
            } => {
                let body = format_analysis_answer(
                    &language,
                    freshness,
                    narrative,
                    numbers,
                    timestamps,
                    &used_evidence,
                );
                let intent = text_reply(&envelope, &body)
                    .ok_or(Disposition::Text("agent.answer_unverified"))?;
                self.database
                    .queue_telegram_diary_analysis(
                        lease,
                        TelegramAnalysisPublication {
                            model: interpretation.publication_context(self.timezone),
                            goals_revision: goals.revision,
                            scoped_sleep_memory: tools
                                .selected_read()
                                .is_some()
                                .then_some(&memory.fence),
                        },
                        &used_evidence,
                        &schema_versions,
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
            Disposition::VoiceUnavailable => {
                let _ = self.database.finish_unavailable_telegram_voice(lease).await;
            }
            Disposition::Text(key) => {
                let _ = self
                    .database
                    .finish_telegram_model_notice(lease, template(language, key))
                    .await;
            }
            Disposition::Provider(error) => {
                let message = provider_notice(language, self.gateway.binding().provider, error);
                let _ = self
                    .database
                    .finish_telegram_model_notice(lease, &message)
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

enum Disposition {
    Gone,
    VoiceUnavailable,
    Retry(Duration),
    Text(&'static str),
    Provider(ModelTransportError),
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
        TelegramDiaryCommitError::VoiceConsentChanged => Disposition::VoiceUnavailable,
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
        AnalysisError::Read(error) => Disposition::Text(error.notice_key()),
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
        GatewayError::Attempt(FallbackError::Provider(error)) => Disposition::Provider(error),
        GatewayError::Attempt(FallbackError::NoEligibleModel) => {
            Disposition::Text("agent.provider_no_model")
        }
        GatewayError::Consent(_) | GatewayError::Attempt(FallbackError::ConsentRequired) => {
            Disposition::Text("agent.data_changed")
        }
        _ => Disposition::Text("agent.answer_unverified"),
    }
}

fn provider_notice(locale: &str, provider: ModelProvider, error: ModelTransportError) -> String {
    let provider = match provider {
        ModelProvider::Gemini => "Gemini",
        ModelProvider::Structured(StructuredProvider::OpenAi) => "OpenAI",
        ModelProvider::Structured(StructuredProvider::Anthropic) => "Anthropic",
        ModelProvider::Extension => template(locale, "agent.provider_extension"),
    };
    let (key, status) = match error {
        ModelTransportError::InvalidConfiguration => ("agent.provider_configuration", None),
        ModelTransportError::RequestRejected(status) => ("agent.provider_rejected", status),
        ModelTransportError::Unavailable { reason, detail, .. } => {
            let key = match (reason, detail) {
                (ModelUnavailability::Authorization, _) => "agent.provider_auth",
                (ModelUnavailability::MissingModel, _) => "agent.provider_missing_model",
                (ModelUnavailability::RateLimited, _) => "agent.provider_rate_limit",
                (_, ModelFailureDetail::Timeout) => "agent.provider_timeout",
                (_, ModelFailureDetail::HttpStatus(status)) if status.as_u16() == 408 => {
                    "agent.provider_timeout"
                }
                (_, ModelFailureDetail::Connection) => "agent.provider_connection",
                (_, ModelFailureDetail::Network) => "agent.provider_network",
                (_, ModelFailureDetail::ProviderResponse) => "agent.provider_incomplete",
                _ => "agent.provider_unavailable",
            };
            let status = match detail {
                ModelFailureDetail::HttpStatus(status) => Some(status),
                _ => None,
            };
            (key, status)
        }
        ModelTransportError::Response(error) => {
            let key = match error {
                ModelResponseError::Refused => "agent.provider_refused",
                ModelResponseError::Incomplete | ModelResponseError::Unavailable => {
                    "agent.provider_incomplete"
                }
                ModelResponseError::TooLarge => "agent.provider_large_response",
                _ => "agent.provider_invalid_response",
            };
            (key, None)
        }
    };
    let reason = template(locale, key);
    match status {
        Some(status) => format!("{reason} ({provider}, HTTP {}).", status.as_u16()),
        None => format!("{reason} ({provider})."),
    }
}

fn budget_disposition(error: AnalysisBudgetError) -> Disposition {
    Disposition::Text(match error {
        AnalysisBudgetError::Limit => "agent.budget",
        AnalysisBudgetError::Timeout => "agent.timeout",
    })
}
