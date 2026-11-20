mod diary;
pub use diary::DiaryAnalysisTools;

use std::{collections::BTreeSet, error::Error, fmt, future::Future, io, time::Instant};

use ga_db::{
    conversation_memory::AnalysisSubject,
    health_analysis::{
        COFFEE_SLEEP, DATA_FRESHNESS, DailyAnalysisEvidence, EVENT_WINDOWS, HEALTH_RANGE,
        HEALTH_SNAPSHOT, LAGGED_ASSOCIATION, METRIC_SERIES, MIGRAINE_WINDOWS, RUNNING_EFFICIENCY,
        SLEEP_ANALYSIS, STRESS_HOURS,
        generic::{GENERIC_ANALYSIS, GenericAnalysisEvidence},
    },
    share_policy::{
        ModelChannelSharing, ModelConversation, ShareCategory, ShareDestination, ShareRequirement,
    },
};
use ga_domain::{
    analysis_budget::{AnalysisBudget, AnalysisBudgetError, AnalysisStage},
    analysis_claims::AnswerEvidence,
    analysis_step::{AnalysisStep, AnalysisStepAction, AnalysisStepError},
    freshness::render_current_state_freshness,
    legacy_json::{JsonError, Utf8Layout, encode},
    localization::catalog_text,
    model_consent::ModelCategories,
    model_request::ApprovedModelSchema,
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use super::{
    ActiveModelJob, CurrentModelPolicy, GatewayError, ModelGateway, ModelInput, Sender,
    runtime::ModelRequestScope,
};

const PROMPT: &str = include_str!("../../../../src/garmin_ai/prompts/analysis_answer.txt");
const MAX_PROMPT_JSON: usize = 96_000;
const MAX_TOOL_ARGUMENTS: usize = 16_000;
const MAX_ROUNDS: u8 = 6;

#[derive(Clone, Copy)]
pub struct OfferedReadTool<'a> {
    pub name: &'a str,
    pub description: &'a Value,
}

pub struct AnalysisQuestion<'a> {
    pub now: &'a str,
    pub timezone: &'a str,
    pub text: &'a str,
    pub locale: &'a str,
    pub conversation: &'a Value,
    pub personal_goals: &'a Value,
    pub replay_notice: Option<&'a str>,
    pub tools: &'a [OfferedReadTool<'a>],
}

#[derive(Clone, Copy)]
pub struct AnalysisDestination<'a> {
    pub channel: ShareDestination<'a>,
    pub subject: AnalysisSubject<'a>,
    pub tracker_versions: &'a BTreeSet<Uuid>,
    pub job: Option<ActiveModelJob>,
    pub uses_voice_text: bool,
}

struct AnalysisScope<'a> {
    requirements: &'a [ShareRequirement<'a>],
    job: Option<ActiveModelJob>,
    destination: Option<AnalysisDestination<'a>>,
}

/// Trusted reader output. Dependency IDs come from storage, outside the model payload.
pub struct AnalysisToolRead {
    pub value: Value,
    pub tracker_versions: BTreeSet<Uuid>,
}

/// Read-only adapter tied to the owner, the offered arguments, and the current
/// revision of the evidence. Tool names in model-provided data grant no access.
pub trait AnalysisReadTools: Sync {
    fn categories(&self) -> ModelCategories;
    fn context_current(&self) -> impl Future<Output = bool> + Send;
    fn read(
        &self,
        name: &str,
        arguments: &Map<String, Value>,
    ) -> impl Future<Output = Option<AnalysisToolRead>> + Send;

    fn quality_report(&self) -> impl Future<Output = Option<(Value, bool)>> + Send {
        std::future::ready(None)
    }
}

pub enum AnalysisResult {
    Safety {
        language: String,
    },
    Replay {
        language: String,
    },
    Answer {
        language: String,
        narrative: Option<String>,
        numbers: Vec<String>,
        timestamps: Vec<String>,
        freshness: Option<String>,
        used_evidence: Vec<Value>,
        tracker_versions: BTreeSet<Uuid>,
    },
    NoAnswer {
        language: String,
    },
}

#[derive(Debug)]
pub enum AnalysisError {
    Budget(AnalysisBudgetError),
    EvidenceChanged,
    Step(AnalysisStepError),
    Model(GatewayError),
    InvalidDocument,
}

impl fmt::Display for AnalysisError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Budget(error) => error.fmt(f),
            Self::Step(error) => error.fmt(f),
            Self::Model(error) => error.fmt(f),
            Self::EvidenceChanged => f.write_str("Analysis context changed during the question"),
            Self::InvalidDocument => f.write_str("Analysis prompt or read evidence is invalid"),
        }
    }
}
impl Error for AnalysisError {}
impl From<AnalysisBudgetError> for AnalysisError {
    fn from(error: AnalysisBudgetError) -> Self {
        Self::Budget(error)
    }
}

impl ModelGateway<'_> {
    pub async fn answer_with_evidence<'policy>(
        &self,
        question: AnalysisQuestion<'_>,
        budget: &mut AnalysisBudget,
        tools: &impl AnalysisReadTools,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
        requirements: &[ShareRequirement<'_>],
        job: Option<ActiveModelJob>,
    ) -> Result<AnalysisResult, AnalysisError> {
        self.answer_scope(
            question,
            budget,
            tools,
            policy,
            AnalysisScope {
                requirements,
                job,
                destination: None,
            },
            &self.transport,
        )
        .await
    }

    pub(crate) async fn answer_for_channel<'policy>(
        &self,
        question: AnalysisQuestion<'_>,
        budget: &mut AnalysisBudget,
        tools: &impl AnalysisReadTools,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
        destination: AnalysisDestination<'_>,
        sender: &impl Sender,
    ) -> Result<AnalysisResult, AnalysisError> {
        self.answer_scope(
            question,
            budget,
            tools,
            policy,
            AnalysisScope {
                requirements: &[],
                job: destination.job,
                destination: Some(destination),
            },
            sender,
        )
        .await
    }

    async fn answer_scope<'policy>(
        &self,
        question: AnalysisQuestion<'_>,
        budget: &mut AnalysisBudget,
        tools: &impl AnalysisReadTools,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
        scope: AnalysisScope<'_>,
        sender: &impl Sender,
    ) -> Result<AnalysisResult, AnalysisError> {
        let categories = tools.categories();
        let AnalysisScope {
            requirements,
            job,
            destination,
        } = scope;
        answer_with(
            question,
            budget,
            tools,
            |prompt, versions, daily, generic| {
                let policy = &policy;
                async move {
                    let mut current_requirements = requirements.to_vec();
                    current_requirements.extend(versions.iter().copied().map(|version_id| {
                        ShareRequirement {
                            version_id,
                            categories: &[ShareCategory::Facts],
                        }
                    }));
                    let mut channel_versions = versions;
                    if let Some(destination) = destination {
                        channel_versions.extend(destination.tracker_versions);
                    }
                    let channel_requirements: Vec<_> = channel_versions
                        .iter()
                        .map(|&version_id| ShareRequirement {
                            version_id,
                            categories: &[ShareCategory::Schema, ShareCategory::Facts],
                        })
                        .collect();
                    let channel = destination.map(|destination| ModelChannelSharing {
                        destination: destination.channel,
                        requirements: &channel_requirements,
                        conversation: Some(ModelConversation::Analysis {
                            subject: destination.subject,
                            daily: &daily,
                            generic: &generic,
                        }),
                    });
                    let returned = self
                        .execute_with_context(
                            ModelInput::Structured {
                                contract: ApprovedModelSchema::AgentStep,
                                instruction: PROMPT.trim(),
                                prompt: &prompt,
                                categories,
                            },
                            policy,
                            ModelRequestScope {
                                uses_voice_text: destination
                                    .is_some_and(|scope| scope.uses_voice_text),
                                requirements: &current_requirements,
                                channel,
                                job,
                            },
                            sender,
                        )
                        .await
                        .map_err(AnalysisError::Model)?;
                    Ok(returned.value)
                }
            },
        )
        .await
    }
}

async fn answer_with<T, F, Response>(
    question: AnalysisQuestion<'_>,
    budget: &mut AnalysisBudget,
    tools: &T,
    mut call_model: F,
) -> Result<AnalysisResult, AnalysisError>
where
    T: AnalysisReadTools,
    F: FnMut(
        String,
        BTreeSet<Uuid>,
        Vec<DailyAnalysisEvidence>,
        Vec<GenericAnalysisEvidence>,
    ) -> Response,
    Response: Future<Output = Result<Value, AnalysisError>>,
{
    let mut evidence: Vec<Value> = Vec::new();
    let mut tracker_versions = BTreeSet::new();
    let mut language = question.locale.to_owned();
    let mut quality = None;
    let mut quality_loaded = false;
    let mut replaying = question.replay_notice.is_some();
    for turn in 0..MAX_ROUNDS {
        check_context(tools, budget).await?;
        let point = matches!(evidence.as_slice(), [item] if
            item["tool"] == SLEEP_ANALYSIS
                && item["arguments"]["timing_only"] == true
                && item["result"]["algorithm_version"] == "main-sleep-timing-v1"
        );
        if turn > 0 && !point && !quality_loaded {
            let remaining = budget.remaining(Instant::now())?;
            if let Some((report, ready)) = tokio::time::timeout(remaining, tools.quality_report())
                .await
                .map_err(|_| AnalysisBudgetError::Timeout)?
            {
                replaying |= !ready;
                if report["channels"]
                    .as_object()
                    .is_some_and(|channels| !channels.is_empty())
                {
                    quality = Some(
                        json!({"tool":DATA_FRESHNESS,"arguments":{},"result":report,"implicit":true}),
                    );
                }
            }
            quality_loaded = true;
        }
        let mut conversation = question.conversation.clone();
        let replay_notice = if replaying {
            if conversation.is_object() {
                conversation["turns"] = json!([]);
            }
            catalog_text(&language, "agent.replay").or(question.replay_notice)
        } else {
            None
        };
        let rounds = budget.remaining_tool_rounds(turn);
        let answer_only = rounds == 0;
        let descriptions: Vec<_> = if answer_only {
            vec![]
        } else {
            question
                .tools
                .iter()
                .filter(|tool| {
                    !replaying
                        || !matches!(
                            tool.name,
                            GENERIC_ANALYSIS
                                | LAGGED_ASSOCIATION
                                | MIGRAINE_WINDOWS
                                | RUNNING_EFFICIENCY
                                | STRESS_HOURS
                                | HEALTH_SNAPSHOT
                                | HEALTH_RANGE
                                | METRIC_SERIES
                                | COFFEE_SLEEP
                                | EVENT_WINDOWS
                        )
                })
                .map(|tool| tool.description)
                .collect()
        };
        let prompt = ordered(&json!({
            "now": question.now,
            "timezone": question.timezone,
            "question": question.text,
            "conversation": conversation,
            "quality_context": quality.as_ref().map_or_else(|| json!({}), |value| value["result"]["channels"].clone()),
            "tools": descriptions,
            "personal_goals": question.personal_goals,
            "remaining_tool_rounds": rounds,
            "remaining_tool_calls": budget.remaining_tool_calls(),
            "answer_only": answer_only,
            "planning_only": turn == 0,
            "garmin_replay_notice": replay_notice,
            "evidence": evidence,
        }))?;
        let remaining = budget.record_model_call(
            AnalysisStage::Answer,
            PROMPT.trim(),
            &prompt,
            Instant::now(),
        )?;
        let daily = evidence
            .iter()
            .chain(quality.iter())
            .filter(|item| {
                item["tool"]
                    .as_str()
                    .is_some_and(DailyAnalysisEvidence::is_daily_tool)
                    && item["result"].get("error").is_none()
            })
            .map(|item| {
                DailyAnalysisEvidence::from_read(item, question.timezone)
                    .ok_or(AnalysisError::InvalidDocument)
            })
            .collect::<Result<Vec<_>, _>>()?;
        let generic = evidence
            .iter()
            .filter(|item| {
                item["tool"] == GENERIC_ANALYSIS && item["result"].get("error").is_none()
            })
            .map(|item| {
                GenericAnalysisEvidence::from_read(item, question.now)
                    .ok_or(AnalysisError::InvalidDocument)
            })
            .collect::<Result<Vec<_>, _>>()?;
        let output = tokio::time::timeout(
            remaining,
            call_model(prompt, tracker_versions.clone(), daily, generic),
        )
        .await
        .map_err(|_| AnalysisBudgetError::Timeout)??;
        let step = AnalysisStep::from_value(&output).map_err(AnalysisError::Step)?;
        let response_language = step.language(&language).to_owned();
        let selected = evidence
            .iter()
            .enumerate()
            .map(|(index, entry)| AnswerEvidence {
                id: (index + 1) as u64,
                tool: entry["tool"].as_str().unwrap_or_default(),
                result: &entry["result"],
            })
            .collect::<Vec<_>>();
        check_context(tools, budget).await?;
        match step
            .action(
                answer_only,
                &selected,
                question.timezone,
                &response_language,
            )
            .map_err(AnalysisError::Step)?
        {
            AnalysisStepAction::Safety { language } => {
                return Ok(AnalysisResult::Safety {
                    language: language.to_owned(),
                });
            }
            AnalysisStepAction::Answer(answer) => {
                if answer.include_current_state_freshness && replaying {
                    return Ok(AnalysisResult::Replay {
                        language: answer.language.to_owned(),
                    });
                }
                let mut selected: Vec<_> = evidence
                    .into_iter()
                    .enumerate()
                    .filter(|(index, _)| answer.selected.contains(&((*index + 1) as u64)))
                    .map(|(_, mut item)| {
                        if item["tool"] == GENERIC_ANALYSIS {
                            item["generic_request_time"] = json!(question.now);
                        }
                        item
                    })
                    .collect();
                let freshness = if answer.include_current_state_freshness {
                    quality.as_ref().and_then(|read| {
                        let at = StoredInstant::from_python_isoformat(question.now)?;
                        let display = render_current_state_freshness(
                            &read["result"]["channels"],
                            at,
                            question.timezone,
                            answer.language,
                        )?;
                        if !display.is_empty() {
                            selected.push(read.clone());
                        }
                        Some(display)
                    })
                } else {
                    None
                };
                return Ok(AnalysisResult::Answer {
                    language: answer.language.to_owned(),
                    narrative: answer.narrative.map(str::to_owned),
                    numbers: answer.numbers,
                    timestamps: answer.timestamps,
                    freshness,
                    used_evidence: selected,
                    tracker_versions,
                });
            }
            AnalysisStepAction::NoAnswer => {
                if budget.remaining_tool_calls() == 0 {
                    return Err(AnalysisBudgetError::Limit.into());
                }
                return Ok(AnalysisResult::NoAnswer {
                    language: response_language,
                });
            }
            AnalysisStepAction::Read(calls) => {
                for request in calls
                    .iter()
                    .take(usize::from(budget.remaining_tool_calls()))
                {
                    check_context(tools, budget).await?;
                    budget.record_tool_call(Instant::now())?;
                    let arguments = if request.arguments_json.len() <= MAX_TOOL_ARGUMENTS {
                        serde_json::from_str::<Map<String, Value>>(request.arguments_json).ok()
                    } else {
                        None
                    };
                    let result = match arguments.as_ref() {
                        Some(args)
                            if question.tools.iter().any(|tool| tool.name == request.name) =>
                        {
                            tokio::time::timeout(
                                budget.remaining(Instant::now())?,
                                tools.read(request.name, args),
                            )
                            .await
                            .map_err(|_| AnalysisBudgetError::Timeout)?
                        }
                        _ => None,
                    };
                    let result = result
                        .filter(|read| read.value.is_object() || read.value.is_array())
                        .map(|read| {
                            tracker_versions.extend(read.tracker_versions);
                            read.value
                        })
                        .unwrap_or_else(
                            || json!({"error":"Invalid tool arguments; inspect schema and retry"}),
                        );
                    budget.check_evidence(&ordered(&result)?)?;
                    evidence.push(json!({
                        "id": evidence.len() + 1,
                        "tool": request.name,
                        "result": result,
                        "arguments": arguments.unwrap_or_default(),
                    }));
                    budget.check_evidence(&ordered(&json!(evidence))?)?;
                }
            }
        }
        language = response_language;
    }
    Ok(AnalysisResult::NoAnswer { language })
}

async fn check_context(
    tools: &impl AnalysisReadTools,
    budget: &AnalysisBudget,
) -> Result<(), AnalysisError> {
    if tokio::time::timeout(budget.remaining(Instant::now())?, tools.context_current())
        .await
        .map_err(|_| AnalysisBudgetError::Timeout)?
    {
        Ok(())
    } else {
        Err(AnalysisError::EvidenceChanged)
    }
}

pub(super) fn ordered(document: &impl serde::Serialize) -> Result<String, AnalysisError> {
    let mut bytes = PromptBytes(Vec::new());
    serde_json::to_writer(&mut bytes, document).map_err(|error| {
        if error.io_error_kind() == Some(io::ErrorKind::FileTooLarge) {
            AnalysisBudgetError::Limit.into()
        } else {
            AnalysisError::InvalidDocument
        }
    })?;
    let output = encode(&bytes.0, Utf8Layout::Ordered, MAX_PROMPT_JSON).map_err(|error| {
        if error == JsonError::InputTooLarge {
            AnalysisBudgetError::Limit.into()
        } else {
            AnalysisError::InvalidDocument
        }
    })?;
    String::from_utf8(output).map_err(|_| AnalysisError::InvalidDocument)
}

struct PromptBytes(Vec<u8>);

impl io::Write for PromptBytes {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > MAX_PROMPT_JSON {
            return Err(io::ErrorKind::FileTooLarge.into());
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests;

pub(crate) fn analysis_conversation(turns: Vec<Value>) -> Value {
    let turns: Vec<_> = turns
        .into_iter()
        .filter(|turn| turn["question"].is_string())
        .map(|turn| {
            let mut context = json!({"question":turn["question"],"asked_at":turn["asked_at"]});
            if let Some(specs) = turn.get("specs") {
                context["specs"] = specs.clone();
            }
            context
        })
        .collect();
    json!({
        "turns": turns,
        "explicit_reply": false,
        "selection_missing": false,
        "authority":"Conversation only; re-query tools for evidence. Prior answers are not facts.",
    })
}

pub(crate) fn format_analysis_answer(
    language: &str,
    freshness: Option<String>,
    narrative: Option<String>,
    numbers: Vec<String>,
    timestamps: Vec<String>,
    evidence: &[Value],
) -> String {
    let body = [
        freshness.unwrap_or_default(),
        narrative.unwrap_or_default(),
        numbers.join("\n"),
        timestamps.join("\n"),
    ]
    .into_iter()
    .filter(|part| !part.is_empty())
    .collect::<Vec<_>>()
    .join("\n\n");
    let message = |key| catalog_text(language, key).unwrap_or_default();
    let mut sources = Vec::new();
    if evidence.iter().any(|item| {
        item["implicit"] != true
            && (item["tool"]
                .as_str()
                .is_some_and(DailyAnalysisEvidence::is_daily_tool)
                || item["tool"] == GENERIC_ANALYSIS)
    }) {
        sources.push("Garmin");
    }
    if evidence.iter().any(|item| {
        item["tool"] == "events"
            || item["tool"] == GENERIC_ANALYSIS
            || item["tool"] == MIGRAINE_WINDOWS
            || item["tool"] == COFFEE_SLEEP
            || item["tool"] == EVENT_WINDOWS
            || (item["tool"] == SLEEP_ANALYSIS
                && item["result"]["algorithm_version"] == "sleep-analysis-v1")
    }) {
        sources.push(message("agent.source.diary"));
    }
    if !sources.is_empty() {
        let connector = if language
            .split('-')
            .next()
            .is_some_and(|part| part.eq_ignore_ascii_case("ru"))
        {
            " и "
        } else {
            " and "
        };
        body + &message("agent.saved_sources").replace("{sources}", &sources.join(connector))
    } else {
        body + message("agent.saved_data")
    }
}
