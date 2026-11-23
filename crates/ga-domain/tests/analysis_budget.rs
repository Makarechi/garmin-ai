use std::{
    env,
    error::Error,
    fs,
    time::{Duration, Instant},
};

use ga_domain::analysis_budget::{AnalysisBudget, AnalysisBudgetError, AnalysisStage};
use serde_json::Value;

#[test]
#[ignore = "compares against the pinned Python budget fixture"]
fn matches_shared_interpretation_and_answer_limits() -> Result<(), Box<dyn Error>> {
    let raw = fs::read(env::var("GA_TEST_ANALYSIS_BUDGET")?)?;
    let fixture: Vec<Value> = serde_json::from_slice(&raw)?;
    let mut comparisons = 0;
    for group in fixture {
        let origin = Instant::now();
        let mut budget = AnalysisBudget::new(origin);
        for call in group["calls"].as_array().ok_or("Missing budget calls")? {
            let stage = match call["stage"].as_str() {
                Some("interpretation") => AnalysisStage::Interpretation,
                Some("answer") => AnalysisStage::Answer,
                _ => return Err("Missing call stage".into()),
            };
            let instruction = call["instruction"].as_str().ok_or("Missing instruction")?;
            let prompt = call["fill"]
                .as_str()
                .ok_or("Missing prompt character")?
                .repeat(usize::try_from(
                    call["length"].as_u64().ok_or("Missing prompt length")?,
                )?);
            let elapsed =
                Duration::from_millis(call["elapsed_ms"].as_u64().ok_or("Missing elapsed time")?);
            let actual =
                match budget.record_model_call(stage, instruction, &prompt, origin + elapsed) {
                    Ok(time) => {
                        assert_eq!(time, Duration::from_secs(120).saturating_sub(elapsed));
                        "accepted"
                    }
                    Err(AnalysisBudgetError::Limit) => "limit",
                    Err(AnalysisBudgetError::Timeout) => "timeout",
                };
            assert_eq!(
                Some(actual),
                call["result"].as_str(),
                "{}: {call}",
                group["name"]
            );
            comparisons += 1;
        }
    }
    println!("{comparisons} paired budget decisions matched");
    Ok(())
}

#[test]
fn reserves_the_final_turn_and_rechecks_tool_and_evidence_limits() -> Result<(), Box<dyn Error>> {
    let origin = Instant::now();
    let mut budget = AnalysisBudget::new(origin);
    assert_eq!(budget.remaining_tool_rounds(0), 5);
    assert_eq!(budget.remaining_tool_rounds(5), 0);
    for _ in 0..5 {
        budget.record_model_call(AnalysisStage::Answer, "", "", origin)?;
    }
    assert_eq!(budget.remaining_tool_rounds(0), 0);
    for _ in 0..12 {
        budget.record_tool_call(origin)?;
    }
    assert_eq!(budget.remaining_tool_calls(), 0);
    assert_eq!(
        budget.record_tool_call(origin),
        Err(AnalysisBudgetError::Limit)
    );
    assert_eq!(budget.check_evidence(&"я".repeat(24_000)), Ok(()));
    assert_eq!(
        budget.check_evidence(&"я".repeat(24_001)),
        Err(AnalysisBudgetError::Limit)
    );
    assert_eq!(
        budget.record_tool_call(origin + Duration::from_secs(120)),
        Err(AnalysisBudgetError::Timeout)
    );
    Ok(())
}
