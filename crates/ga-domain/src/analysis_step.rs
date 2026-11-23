//! Validate one model step before executing tools or composing an answer.

mod numeric;

use std::{collections::BTreeSet, error::Error, fmt, sync::OnceLock};

use jsonschema::Validator;
use serde_json::Value;

use crate::{
    analysis_claims::{AnswerEvidence, ClaimError, verified_numbers, verified_timestamps},
    language_tags::normalize_language_tag,
    model_request::ApprovedModelSchema,
    model_response::application_validator,
};

pub struct ReadCall<'a> {
    pub name: &'a str,
    pub arguments_json: &'a str,
}

pub struct AnalysisStep<'a> {
    document: &'a Value,
    response_language: Option<String>,
    calls: Vec<ReadCall<'a>>,
    selected: BTreeSet<u64>,
}

pub struct VerifiedAnalysis<'a> {
    pub language: &'a str,
    pub narrative: Option<&'a str>,
    pub numbers: Vec<String>,
    pub timestamps: Vec<String>,
    pub selected: &'a BTreeSet<u64>,
    pub include_current_state_freshness: bool,
}

pub enum AnalysisStepAction<'a> {
    Safety { language: &'a str },
    Read(&'a [ReadCall<'a>]),
    Answer(VerifiedAnalysis<'a>),
    NoAnswer,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AnalysisStepError {
    Contract,
    InvalidResponse,
    MissingEvidence,
    UnverifiedNarrative,
    Claim(ClaimError),
}

impl fmt::Display for AnalysisStepError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Contract => f.write_str("Analysis result contract is unavailable"),
            Self::InvalidResponse => f.write_str("Analysis result failed validation"),
            Self::MissingEvidence => f.write_str("The answer needs verified evidence"),
            Self::UnverifiedNarrative => f.write_str("The answer contains an unverified number"),
            Self::Claim(error) => error.fmt(f),
        }
    }
}
impl Error for AnalysisStepError {}

impl<'a> AnalysisStep<'a> {
    pub fn from_value(value: &'a Value) -> Result<Self, AnalysisStepError> {
        if !contract()?.is_valid(value) {
            return Err(AnalysisStepError::InvalidResponse);
        }
        let calls = items(&value["calls"])
            .iter()
            .map(|call| {
                Ok(ReadCall {
                    name: call["name"]
                        .as_str()
                        .ok_or(AnalysisStepError::InvalidResponse)?,
                    arguments_json: call["arguments_json"]
                        .as_str()
                        .ok_or(AnalysisStepError::InvalidResponse)?,
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let selected = items(&value["evidence_ids"])
            .iter()
            .map(|id| id.as_u64().ok_or(AnalysisStepError::InvalidResponse))
            .collect::<Result<_, _>>()?;
        let response_language = value["response_language"]
            .as_str()
            .map(|tag| normalize_language_tag(tag).ok_or(AnalysisStepError::InvalidResponse))
            .transpose()?;
        Ok(Self {
            document: value,
            response_language,
            calls,
            selected,
        })
    }

    #[must_use]
    pub fn language<'step>(&'step self, fallback: &'step str) -> &'step str {
        self.response_language.as_deref().unwrap_or(fallback)
    }

    /// Select a single next step. Urgency overrides reads and final answers.
    pub fn action<'step>(
        &'step self,
        answer_only: bool,
        evidence: &[AnswerEvidence<'_>],
        timezone: &str,
        locale: &'step str,
    ) -> Result<AnalysisStepAction<'step>, AnalysisStepError> {
        let language = self.language(locale);
        if self.document["urgent_safety"] == true {
            return Ok(AnalysisStepAction::Safety { language });
        }
        if !self.calls.is_empty() {
            return Ok(if answer_only {
                AnalysisStepAction::NoAnswer
            } else {
                AnalysisStepAction::Read(&self.calls)
            });
        }
        let narrative = self.document["answer"]
            .as_str()
            .filter(|text| !text.is_empty());
        let numbers = items(&self.document["numeric_claims"]);
        let timestamps = items(&self.document["timestamp_claims"]);
        if narrative.is_none() && numbers.is_empty() && timestamps.is_empty() {
            return Ok(AnalysisStepAction::NoAnswer);
        }
        if self.selected.is_empty()
            || !self.selected.iter().all(|id| {
                evidence
                    .iter()
                    .any(|item| item.id == *id && item.result.get("error").is_none())
            })
        {
            return Err(AnalysisStepError::MissingEvidence);
        }
        if narrative.is_some_and(|answer| answer.chars().any(numeric::is_numeric)) {
            return Err(AnalysisStepError::UnverifiedNarrative);
        }
        let numbers = verified_numbers(numbers, evidence, &self.selected)
            .map_err(AnalysisStepError::Claim)?;
        let timestamps =
            verified_timestamps(timestamps, evidence, &self.selected, timezone, language)
                .map_err(AnalysisStepError::Claim)?;
        Ok(AnalysisStepAction::Answer(VerifiedAnalysis {
            language,
            narrative,
            numbers,
            timestamps,
            selected: &self.selected,
            include_current_state_freshness: self.document["include_current_state_freshness"]
                == true,
        }))
    }
}

fn contract() -> Result<&'static Validator, AnalysisStepError> {
    static SCHEMA: OnceLock<Option<Validator>> = OnceLock::new();
    SCHEMA
        .get_or_init(|| {
            let original =
                serde_json::from_slice(ApprovedModelSchema::AgentStep.document().ok()?).ok()?;
            application_validator(&original).ok()
        })
        .as_ref()
        .ok_or(AnalysisStepError::Contract)
}

fn items(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}
