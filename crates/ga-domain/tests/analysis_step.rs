use std::{env, error::Error, fs};

use ga_domain::{
    analysis_claims::AnswerEvidence,
    analysis_step::{AnalysisStep, AnalysisStepAction, AnalysisStepError},
};
use serde_json::{Value, json};

#[test]
#[ignore = "compares synthetic model steps to the pinned Python answer loop"]
fn guards_answer_and_tool_selection() -> Result<(), Box<dyn Error>> {
    let records: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_ANALYSIS_STEP")?)?)?;
    for case in &records {
        let evidence = if case["evidence"].is_null() {
            vec![]
        } else {
            vec![AnswerEvidence {
                id: 1,
                tool: "personal_baseline",
                result: &case["evidence"],
            }]
        };
        let step = AnalysisStep::from_value(&case["document"])?;
        let result = match step.action(false, &evidence, "Europe/Bratislava", "ru") {
            Ok(AnalysisStepAction::Answer(answer)) => {
                assert_eq!(Some(answer.language), case["language"].as_str());
                assert!(answer.selected.contains(&1));
                if case["name"] == "numeric_only" {
                    assert_eq!(answer.numbers, ["Evidence 1: personal_baseline /mean: 78"]);
                }
                "verified"
            }
            Ok(AnalysisStepAction::Safety { language }) => {
                assert_eq!(Some(language), case["language"].as_str());
                "safety"
            }
            Ok(AnalysisStepAction::NoAnswer) => "empty",
            Ok(AnalysisStepAction::Read(_)) => "read",
            Err(AnalysisStepError::MissingEvidence) => "missing_evidence",
            Err(AnalysisStepError::UnverifiedNarrative | AnalysisStepError::Claim(_)) => {
                "unverified"
            }
            Err(error) => return Err(error.into()),
        };
        assert_eq!(Some(result), case["result"].as_str(), "{}", case["name"]);
    }
    println!(
        "{} final analysis step decisions matched Python",
        records.len()
    );
    Ok(())
}

#[test]
fn final_round_cannot_execute_tools() -> Result<(), Box<dyn Error>> {
    let document = json!({
        "response_language":"en",
        "answer":"Unverified narrative",
        "calls": [{"name":"analysis_sleep","arguments_json":"{}"}],
    });
    let step = AnalysisStep::from_value(&document)?;
    assert!(matches!(
        step.action(true, &[], "UTC", "ru")?,
        AnalysisStepAction::NoAnswer
    ));
    let AnalysisStepAction::Read(calls) = step.action(false, &[], "UTC", "ru")? else {
        return Err("Expected read calls before the final round".into());
    };
    assert_eq!(calls[0].name, "analysis_sleep");
    assert_eq!(calls[0].arguments_json, "{}");
    let invalid = json!({"calls":[{"name":"untrusted_tool","arguments_json":{}}]});
    assert!(matches!(
        AnalysisStep::from_value(&invalid),
        Err(AnalysisStepError::InvalidResponse)
    ));
    Ok(())
}
