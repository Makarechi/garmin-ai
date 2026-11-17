//! Local validation of the structured diary interpretation returned by a model.

use std::{collections::BTreeSet, error::Error, fmt, sync::OnceLock};

use jsonschema::Validator;
use serde_json::Value;
use uuid::Uuid;

use crate::{
    event_input::{EventInput, EventStatus, InvalidEvent, ValidationMode},
    language_tags::normalize_language_tag,
    model_request::ApprovedModelSchema,
    model_response::{MAX_RESPONSE_BYTES, application_validator},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AgentIntent {
    Log,
    Update,
    Close,
    Undo,
    Question,
    Clarify,
    Safety,
    Acknowledge,
}

impl AgentIntent {
    fn parse(value: &Value) -> Option<Self> {
        Some(match value.as_str()? {
            "log" => Self::Log,
            "update" => Self::Update,
            "close" => Self::Close,
            "undo" => Self::Undo,
            "question" => Self::Question,
            "clarify" => Self::Clarify,
            "safety" => Self::Safety,
            "acknowledge" => Self::Acknowledge,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DraftLink {
    pub child_index: usize,
    pub parent_index: usize,
}

pub struct AgentInterpretation {
    intent: AgentIntent,
    events: Vec<EventInput>,
    draft_links: Vec<DraftLink>,
    target_event_id: Option<Uuid>,
    target_question_id: Option<Uuid>,
    question_context_ids: Vec<Uuid>,
    clarification: Option<String>,
    confidence: f64,
    response_language: Option<String>,
    changed_fields: Vec<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InterpretationError {
    Contract,
    InvalidResponse,
    InvalidEvent(InvalidEvent),
    MissingEvents,
    TargetRequired,
    InvalidDraftLinks,
    InvalidLanguage,
}

impl fmt::Display for InterpretationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Contract => f.write_str("Diary interpretation contract is unavailable"),
            Self::InvalidResponse => f.write_str("Diary interpretation failed validation"),
            Self::InvalidEvent(error) => error.fmt(f),
            Self::MissingEvents => f.write_str("Diary mutation requires validated event data"),
            Self::TargetRequired => f.write_str("Diary change requires one target event"),
            Self::InvalidDraftLinks => f.write_str("Diary draft relationships are invalid"),
            Self::InvalidLanguage => f.write_str("Diary interpretation language is invalid"),
        }
    }
}
impl Error for InterpretationError {}

impl AgentInterpretation {
    pub fn from_value(value: &Value) -> Result<Self, InterpretationError> {
        if !contract()?.is_valid(value) {
            return Err(InterpretationError::InvalidResponse);
        }
        let intent =
            AgentIntent::parse(&value["intent"]).ok_or(InterpretationError::InvalidResponse)?;
        let confidence = value["confidence"]
            .as_f64()
            .ok_or(InterpretationError::InvalidResponse)?;
        let events = items(&value["events"])
            .iter()
            .map(|event| {
                let bytes =
                    serde_json::to_vec(event).map_err(|_| InterpretationError::InvalidResponse)?;
                EventInput::from_json(&bytes, MAX_RESPONSE_BYTES, ValidationMode::NewReport)
                    .map_err(InterpretationError::InvalidEvent)
            })
            .collect::<Result<Vec<_>, _>>()?;
        if matches!(
            intent,
            AgentIntent::Log | AgentIntent::Update | AgentIntent::Close
        ) && events.is_empty()
        {
            return Err(InterpretationError::MissingEvents);
        }
        let target_event_id = optional_uuid(&value["target_event_id"])?;
        if matches!(intent, AgentIntent::Update | AgentIntent::Close) && target_event_id.is_none() {
            return Err(InterpretationError::TargetRequired);
        }
        let draft_links = draft_links(intent, &events, items(&value["draft_links"]))?;
        let question_context_ids = items(&value["question_context_ids"])
            .iter()
            .map(|id| optional_uuid(id)?.ok_or(InterpretationError::InvalidResponse))
            .collect::<Result<_, _>>()?;
        let response_language = value["response_language"]
            .as_str()
            .map(|tag| normalize_language_tag(tag).ok_or(InterpretationError::InvalidLanguage))
            .transpose()?;
        Ok(Self {
            intent,
            confidence,
            events,
            draft_links,
            target_event_id,
            question_context_ids,
            response_language,
            target_question_id: optional_uuid(&value["target_question_id"])?,
            clarification: value["clarification"].as_str().map(str::to_owned),
            changed_fields: items(&value["changed_fields"])
                .iter()
                .map(|item| {
                    item.as_str()
                        .map(str::to_owned)
                        .ok_or(InterpretationError::InvalidResponse)
                })
                .collect::<Result<_, _>>()?,
        })
    }

    pub fn intent(&self) -> AgentIntent {
        self.intent
    }
    pub fn events(&self) -> &[EventInput] {
        &self.events
    }
    pub fn draft_links(&self) -> &[DraftLink] {
        &self.draft_links
    }
    pub fn target_event_id(&self) -> Option<Uuid> {
        self.target_event_id
    }
    pub fn target_question_id(&self) -> Option<Uuid> {
        self.target_question_id
    }
    pub fn question_context_ids(&self) -> &[Uuid] {
        &self.question_context_ids
    }
    pub fn clarification(&self) -> Option<&str> {
        self.clarification.as_deref()
    }
    pub fn confidence(&self) -> f64 {
        self.confidence
    }
    pub fn response_language(&self) -> Option<&str> {
        self.response_language.as_deref()
    }
    pub fn changed_fields(&self) -> &[String] {
        &self.changed_fields
    }
}

fn contract() -> Result<&'static Validator, InterpretationError> {
    static SCHEMA: OnceLock<Option<Validator>> = OnceLock::new();
    SCHEMA
        .get_or_init(|| {
            let original =
                serde_json::from_slice(ApprovedModelSchema::Interpretation.document()).ok()?;
            application_validator(&original).ok()
        })
        .as_ref()
        .ok_or(InterpretationError::Contract)
}

fn items(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

fn optional_uuid(value: &Value) -> Result<Option<Uuid>, InterpretationError> {
    value
        .as_str()
        .map(|text| {
            text.parse()
                .map_err(|_| InterpretationError::InvalidResponse)
        })
        .transpose()
}

fn draft_links(
    intent: AgentIntent,
    events: &[EventInput],
    links: &[Value],
) -> Result<Vec<DraftLink>, InterpretationError> {
    if !links.is_empty() && intent != AgentIntent::Log {
        return Err(InterpretationError::InvalidDraftLinks);
    }
    let mut children = BTreeSet::new();
    links
        .iter()
        .map(|link| {
            let child_index = batch_index(&link["child_index"], events.len())?;
            let parent_index = batch_index(&link["parent_index"], events.len())?;
            let child = &events[child_index];
            let parent = &events[parent_index];
            if !children.insert(child_index)
                || child.payload().kind() != "medication"
                || parent.payload().kind() != "migraine"
                || !child.payload().json()["reason_event_id"].is_null()
                || parent.status() != EventStatus::Confirmed
            {
                return Err(InterpretationError::InvalidDraftLinks);
            }
            Ok(DraftLink {
                child_index,
                parent_index,
            })
        })
        .collect()
}

fn batch_index(value: &Value, len: usize) -> Result<usize, InterpretationError> {
    value
        .as_u64()
        .and_then(|index| usize::try_from(index).ok())
        .filter(|index| *index < len)
        .ok_or(InterpretationError::InvalidDraftLinks)
}
