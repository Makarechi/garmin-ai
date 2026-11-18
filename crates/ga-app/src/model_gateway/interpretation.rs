use std::{collections::BTreeSet, error::Error, fmt, time::Instant};

use ga_db::share_policy::ShareRequirement;
use ga_domain::{
    agent_interpretation::{AgentIntent, AgentInterpretation, InterpretationError},
    analysis_budget::{AnalysisBudget, AnalysisBudgetError, AnalysisStage},
    event_input::{EventSource, source_offset_datetime},
    model_consent::ModelCategories,
    model_request::ApprovedModelSchema,
    timestamp::StoredInstant,
};
#[cfg(test)]
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ActiveModelJob, AnalysisError, CurrentModelPolicy, GatewayError, ModelGateway, ModelInput,
    analysis::ordered,
};

const EXTRACT: &str = include_str!("../../../../src/garmin_ai/prompts/diary_extract.txt");
const OWNER_TEXT_LIMIT: usize = 16_000;
const PROMPT_LIMIT: usize = 24_000;
const EVENT_LIMIT: usize = 20;
const SUMMARY_LIMIT: usize = 300;

/// Already filtered for the selected owner, channel, model and scenario packs.
#[derive(Default, Serialize)]
#[cfg_attr(test, derive(Deserialize))]
pub struct ModelDiaryContext {
    pub recent_analysis_question: Option<Value>,
    pub recent_events: Vec<Value>,
    pub history_truncated: bool,
    pub pending_clarification: Option<Value>,
    pub recent_questions: Vec<Value>,
}

pub struct DiaryInterpretationRequest<'a> {
    pub now: StoredInstant,
    pub timezone: &'a str,
    pub source: EventSource,
    pub text: &'a str,
    pub context: ModelDiaryContext,
}

#[derive(Debug)]
pub enum DiaryInterpretationError {
    RequiresSafetyScreen,
    HistoryTooLarge,
    InvalidContext,
    Budget(AnalysisBudgetError),
    Model(GatewayError),
    Response(InterpretationError),
    UnofferedTarget,
}

impl fmt::Display for DiaryInterpretationError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::RequiresSafetyScreen => {
                output.write_str("Owner text requires the bounded safety screen")
            }
            Self::HistoryTooLarge => {
                output.write_str("Diary context exceeds the model prompt limit")
            }
            Self::InvalidContext => output.write_str("The offered diary context is invalid"),
            Self::Budget(error) => error.fmt(output),
            Self::Model(error) => error.fmt(output),
            Self::Response(error) => error.fmt(output),
            Self::UnofferedTarget => {
                output.write_str("Model referred to a record outside the offered diary context")
            }
        }
    }
}
impl Error for DiaryInterpretationError {}
impl From<AnalysisBudgetError> for DiaryInterpretationError {
    fn from(error: AnalysisBudgetError) -> Self {
        Self::Budget(error)
    }
}

impl ModelGateway<'_> {
    /// Classify one owner message against the exact records offered in the prompt.
    pub async fn interpret_diary_message<'policy>(
        &self,
        request: DiaryInterpretationRequest<'_>,
        budget: &mut AnalysisBudget,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
        requirements: &[ShareRequirement<'_>],
        job: Option<ActiveModelJob>,
    ) -> Result<AgentInterpretation, DiaryInterpretationError> {
        let prompt = PreparedPrompt::new(request)?;
        let remaining = budget.record_model_call(
            AnalysisStage::Interpretation,
            EXTRACT.trim(),
            &prompt.json,
            Instant::now(),
        )?;
        let response = tokio::time::timeout(
            remaining,
            self.execute(
                ModelInput::Structured {
                    contract: ApprovedModelSchema::Interpretation,
                    instruction: EXTRACT.trim(),
                    prompt: &prompt.json,
                    categories: ModelCategories::HEALTH_AND_DIARY,
                },
                policy,
                requirements,
                job,
            ),
        )
        .await
        .map_err(|_| AnalysisBudgetError::Timeout)?
        .map_err(DiaryInterpretationError::Model)?;
        prompt.accept(response.value)
    }
}

#[derive(Serialize)]
struct Prompt<'a> {
    now: String,
    timezone: &'a str,
    source: &'static str,
    context: PromptContext,
    text: &'a str,
}

#[derive(Serialize)]
struct PromptContext {
    #[serde(flatten)]
    offered: ModelDiaryContext,
    open_migraine_count: usize,
    open_episode_counts: OpenEpisodes,
}

#[derive(Serialize, Default)]
struct OpenEpisodes {
    illness: usize,
    migraine: usize,
}

struct PreparedPrompt {
    json: String,
    events: BTreeSet<Uuid>,
    questions: BTreeSet<Uuid>,
}

impl PreparedPrompt {
    fn new(request: DiaryInterpretationRequest<'_>) -> Result<Self, DiaryInterpretationError> {
        use DiaryInterpretationError::{HistoryTooLarge, InvalidContext, RequiresSafetyScreen};
        if request.text.chars().take(OWNER_TEXT_LIMIT + 1).count() > OWNER_TEXT_LIMIT {
            return Err(RequiresSafetyScreen);
        }
        let now = source_offset_datetime(request.now, request.timezone).ok_or(InvalidContext)?;
        let mut offered = request.context;
        offered.history_truncated |= offered.recent_events.len() > EVENT_LIMIT;
        let mut episodes = OpenEpisodes::default();
        let mut open_migraine_count = 0;
        for event in &mut offered.recent_events {
            event
                .as_object_mut()
                .ok_or(InvalidContext)?
                .remove("original_text");
            if open_at(event, request.now)? {
                if event["kind"] == "migraine" {
                    open_migraine_count += 1;
                }
                if event["status"] == "confirmed" {
                    match event["kind"].as_str() {
                        Some("migraine") => episodes.migraine += 1,
                        Some("illness") => episodes.illness += 1,
                        _ => {}
                    }
                }
            }
        }
        offered.recent_events.sort_by_cached_key(|event| {
            let only_open = match event["kind"].as_str() {
                Some("migraine") => episodes.migraine == 1,
                Some("illness") => episodes.illness == 1,
                _ => false,
            };
            !(only_open
                && event["status"] == "confirmed"
                && matches!(open_at(event, request.now), Ok(true)))
        });
        offered.recent_events.truncate(EVENT_LIMIT);
        for record in offered
            .recent_events
            .iter_mut()
            .chain(&mut offered.recent_questions)
        {
            summarize(record);
        }
        let mut prompt = Prompt {
            now: local_time(now),
            timezone: request.timezone,
            source: request.source.label(),
            context: PromptContext {
                offered,
                open_migraine_count,
                open_episode_counts: episodes,
            },
            text: request.text,
        };
        let json = match encode_prompt(&prompt)? {
            Some(json) => json,
            None => {
                prompt.context.offered.history_truncated = true;
                prompt.context.offered.recent_events.clear();
                prompt.context.offered.pending_clarification = None;
                encode_prompt(&prompt)?.ok_or(HistoryTooLarge)?
            }
        };
        let offered = prompt.context.offered;
        Ok(Self {
            json,
            events: identities(&offered.recent_events)?,
            questions: identities(&offered.recent_questions)?,
        })
    }

    fn accept(self, value: Value) -> Result<AgentInterpretation, DiaryInterpretationError> {
        let command =
            AgentInterpretation::from_value(&value).map_err(DiaryInterpretationError::Response)?;
        if command.intent() == AgentIntent::Safety {
            // Safety decisions never use offered record references.
            return Ok(command);
        }
        let unoffered_event_link = command.events().iter().any(|event| {
            let payload = event.payload().json();
            ["reason_event_id", "episode_id"].iter().any(|field| {
                let value = &payload[*field];
                !value.is_null()
                    && value
                        .as_str()
                        .and_then(|id| id.parse::<Uuid>().ok())
                        .is_none_or(|id| !self.events.contains(&id))
            })
        });
        if command
            .target_event_id()
            .is_some_and(|id| !self.events.contains(&id))
            || command
                .target_question_id()
                .is_some_and(|id| !self.questions.contains(&id))
            || command
                .question_context_ids()
                .iter()
                .any(|id| !self.questions.contains(id))
            || unoffered_event_link
        {
            return Err(DiaryInterpretationError::UnofferedTarget);
        }
        Ok(command)
    }
}

#[cfg(test)]
mod tests;

fn encode_prompt(prompt: &Prompt<'_>) -> Result<Option<String>, DiaryInterpretationError> {
    match ordered(prompt) {
        Ok(json) if json.chars().take(PROMPT_LIMIT + 1).count() <= PROMPT_LIMIT => Ok(Some(json)),
        Ok(_) | Err(AnalysisError::Budget(AnalysisBudgetError::Limit)) => Ok(None),
        Err(_) => Err(DiaryInterpretationError::InvalidContext),
    }
}

fn open_at(event: &Value, now: StoredInstant) -> Result<bool, DiaryInterpretationError> {
    match event.get("end") {
        Some(Value::Null) => Ok(true),
        Some(end) => StoredInstant::from_legacy_input(end)
            .map(|end| end > now)
            .ok_or(DiaryInterpretationError::InvalidContext),
        None => Err(DiaryInterpretationError::InvalidContext),
    }
}

fn identities(rows: &[Value]) -> Result<BTreeSet<Uuid>, DiaryInterpretationError> {
    rows.iter()
        .map(|row| {
            row["id"]
                .as_str()
                .and_then(|id| id.parse().ok())
                .ok_or(DiaryInterpretationError::InvalidContext)
        })
        .collect()
}

fn summarize(value: &mut Value) {
    match value {
        Value::String(text) => {
            if let Some((extra, _)) = text.char_indices().nth(SUMMARY_LIMIT) {
                text.truncate(extra);
                text.push_str(" [truncated]");
            }
        }
        Value::Array(items) => items.iter_mut().for_each(summarize),
        Value::Object(fields) => fields.values_mut().for_each(summarize),
        _ => {}
    }
}

fn local_time(now: OffsetDateTime) -> String {
    let (hour, minute, second, micros) = now.to_hms_micro();
    let mut text = format!("{}T{hour:02}:{minute:02}:{second:02}", now.date());
    if micros != 0 {
        text.push_str(&format!(".{micros:06}"));
    }
    let offset = now.offset();
    let sign = if offset.is_negative() { '-' } else { '+' };
    let (hours, minutes, seconds) = offset.as_hms();
    text.push_str(&format!(
        "{sign}{:02}:{:02}",
        hours.unsigned_abs(),
        minutes.unsigned_abs()
    ));
    if seconds != 0 {
        text.push_str(&format!(":{:02}", seconds.unsigned_abs()));
    }
    text
}
