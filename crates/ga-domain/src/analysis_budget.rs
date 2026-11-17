//! Shared limits for the interpretation and read-tool answer for one message.

use std::{
    error::Error,
    fmt,
    time::{Duration, Instant},
};

const MAX_CALLS: u8 = 6;
const MAX_TOOL_CALLS: u8 = 12;
const MAX_PROMPT_BYTES: usize = 96_000;
const MAX_TOTAL_INPUT_BYTES: usize = 384_000;
const MAX_EVIDENCE_BYTES: usize = 48_000;
const MAX_TIME: Duration = Duration::from_secs(120);

#[derive(Clone, Copy)]
pub enum AnalysisStage {
    Interpretation,
    Answer,
}

impl AnalysisStage {
    fn schema_bytes(self) -> usize {
        // Bytes in the pinned Python JSON schema serialized with json.dumps.
        // The paired budget fixture checks the boundary for both schemas.
        match self {
            Self::Interpretation => 10_024,
            Self::Answer => 2_340,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AnalysisBudgetError {
    Limit,
    Timeout,
}

impl fmt::Display for AnalysisBudgetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Limit => "Analysis reached its work limit",
            Self::Timeout => "Analysis reached its time limit",
        })
    }
}
impl Error for AnalysisBudgetError {}

pub struct AnalysisBudget {
    started: Instant,
    model_calls: u8,
    input_bytes: usize,
    tool_calls: u8,
}

impl AnalysisBudget {
    pub fn new(started: Instant) -> Self {
        Self {
            started,
            model_calls: 0,
            input_bytes: 0,
            tool_calls: 0,
        }
    }

    /// Check both aggregate and per-call input before a provider request.
    pub fn record_model_call(
        &mut self,
        stage: AnalysisStage,
        instruction: &str,
        prompt: &str,
        now: Instant,
    ) -> Result<Duration, AnalysisBudgetError> {
        let remaining = self.remaining(now)?;
        let requested = instruction
            .len()
            .saturating_add(prompt.len())
            .saturating_add(stage.schema_bytes());
        if self.model_calls >= MAX_CALLS
            || requested > MAX_PROMPT_BYTES
            || self.input_bytes.saturating_add(requested) > MAX_TOTAL_INPUT_BYTES
        {
            return Err(AnalysisBudgetError::Limit);
        }
        self.model_calls += 1;
        self.input_bytes += requested;
        Ok(remaining)
    }

    pub fn remaining(&self, now: Instant) -> Result<Duration, AnalysisBudgetError> {
        let elapsed = now.saturating_duration_since(self.started);
        if elapsed >= MAX_TIME {
            Err(AnalysisBudgetError::Timeout)
        } else {
            Ok(MAX_TIME - elapsed)
        }
    }

    /// Reserve one read attempt, including an invalid tool call.
    pub fn record_tool_call(&mut self, now: Instant) -> Result<(), AnalysisBudgetError> {
        self.remaining(now)?;
        if self.tool_calls >= MAX_TOOL_CALLS {
            return Err(AnalysisBudgetError::Limit);
        }
        self.tool_calls += 1;
        Ok(())
    }

    pub fn remaining_tool_calls(&self) -> u8 {
        MAX_TOOL_CALLS - self.tool_calls
    }

    pub fn remaining_tool_rounds(&self, answer_turn: u8) -> u8 {
        if answer_turn >= MAX_CALLS - 1 || self.remaining_tool_calls() == 0 {
            return 0;
        }
        (MAX_CALLS - 1).saturating_sub(self.model_calls)
    }

    /// Check the complete evidence JSON that will be placed in the next prompt.
    pub fn check_evidence(&self, evidence_json: &str) -> Result<(), AnalysisBudgetError> {
        if evidence_json.len() > MAX_EVIDENCE_BYTES {
            Err(AnalysisBudgetError::Limit)
        } else {
            Ok(())
        }
    }
}
