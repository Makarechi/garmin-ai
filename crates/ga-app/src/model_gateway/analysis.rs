mod diary;
pub use diary::DiaryAnalysisTools;

use std::{collections::BTreeSet, error::Error, fmt, future::Future, io, time::Instant};

use ga_db::share_policy::{ShareCategory, ShareRequirement};
use ga_domain::{
    analysis_budget::{AnalysisBudget, AnalysisBudgetError, AnalysisStage},
    analysis_claims::AnswerEvidence,
    analysis_step::{AnalysisStep, AnalysisStepAction, AnalysisStepError},
    legacy_json::{JsonError, Utf8Layout, encode},
    model_consent::ModelCategories,
    model_request::ApprovedModelSchema,
};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use super::{ActiveModelJob, CurrentModelPolicy, GatewayError, ModelGateway, ModelInput};

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
    pub quality_context: &'a Value,
    pub personal_goals: &'a Value,
    pub replay_notice: Option<&'a str>,
    pub tools: &'a [OfferedReadTool<'a>],
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
}

pub enum AnalysisResult {
    Safety {
        language: String,
    },
    Answer {
        language: String,
        narrative: Option<String>,
        numbers: Vec<String>,
        timestamps: Vec<String>,
        include_current_state_freshness: bool,
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
        let categories = tools.categories();
        answer_with(question, budget, tools, |prompt, versions| {
            let policy = &policy;
            async move {
                let mut current_requirements = requirements.to_vec();
                current_requirements.extend(versions.into_iter().map(|version_id| {
                    ShareRequirement {
                        version_id,
                        categories: &[ShareCategory::Facts],
                    }
                }));
                let returned = self
                    .execute(
                        ModelInput::Structured {
                            contract: ApprovedModelSchema::AgentStep,
                            instruction: PROMPT.trim(),
                            prompt: &prompt,
                            categories,
                        },
                        policy,
                        &current_requirements,
                        job,
                    )
                    .await
                    .map_err(AnalysisError::Model)?;
                Ok(returned.value)
            }
        })
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
    F: FnMut(String, BTreeSet<Uuid>) -> Response,
    Response: Future<Output = Result<Value, AnalysisError>>,
{
    let mut evidence: Vec<Value> = Vec::new();
    let mut tracker_versions = BTreeSet::new();
    let mut language = question.locale.to_owned();
    for turn in 0..MAX_ROUNDS {
        check_context(tools, budget).await?;
        let rounds = budget.remaining_tool_rounds(turn);
        let answer_only = rounds == 0;
        let descriptions: Vec<_> = if answer_only {
            vec![]
        } else {
            question.tools.iter().map(|tool| tool.description).collect()
        };
        let prompt = ordered(&json!({
            "now": question.now,
            "timezone": question.timezone,
            "question": question.text,
            "conversation": question.conversation,
            "quality_context": question.quality_context,
            "tools": descriptions,
            "personal_goals": question.personal_goals,
            "remaining_tool_rounds": rounds,
            "remaining_tool_calls": budget.remaining_tool_calls(),
            "answer_only": answer_only,
            "garmin_replay_notice": question.replay_notice,
            "evidence": evidence,
        }))?;
        let remaining = budget.record_model_call(
            AnalysisStage::Answer,
            PROMPT.trim(),
            &prompt,
            Instant::now(),
        )?;
        let output = tokio::time::timeout(remaining, call_model(prompt, tracker_versions.clone()))
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
                return Ok(AnalysisResult::Answer {
                    language: answer.language.to_owned(),
                    narrative: answer.narrative.map(str::to_owned),
                    numbers: answer.numbers,
                    timestamps: answer.timestamps,
                    include_current_state_freshness: answer.include_current_state_freshness,
                    used_evidence: evidence
                        .into_iter()
                        .enumerate()
                        .filter(|(index, _)| answer.selected.contains(&((*index + 1) as u64)))
                        .map(|(_, item)| item)
                        .collect(),
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
