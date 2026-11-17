use std::{collections::BTreeSet, env, error::Error, fs};

use ga_domain::analysis_claims::{
    AnswerEvidence, ClaimError, verified_numbers, verified_timestamps,
};
use serde_json::{Value, json};

#[test]
#[ignore = "requires synthetic reference results captured from pinned Python"]
fn verifies_exact_numbers_and_local_times() -> Result<(), Box<dyn Error>> {
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_ANALYSIS_CLAIMS")?)?)?;
    for case in &cases {
        let evidence: Vec<_> = case["evidence"]
            .as_array()
            .ok_or("Missing evidence")?
            .iter()
            .map(|item| -> Result<_, Box<dyn Error>> {
                Ok(AnswerEvidence {
                    id: item["id"].as_u64().ok_or("Missing evidence ID")?,
                    tool: item["tool"].as_str().ok_or("Missing trusted tool name")?,
                    result: &item["result"],
                })
            })
            .collect::<Result<_, _>>()?;
        let claims = case["claims"].as_array().ok_or("Missing claims")?;
        let selected: BTreeSet<u64> = serde_json::from_value(case["selected"].clone())?;
        let actual = if let Some(locale) = case["locale"].as_str() {
            let timezone = case["timezone"].as_str().ok_or("Missing owner timezone")?;
            verified_timestamps(claims, &evidence, &selected, timezone, locale)
        } else {
            verified_numbers(claims, &evidence, &selected)
        };
        assert_eq!(json!(actual.ok()), case["result"], "{}", case["name"]);
    }
    println!(
        "{} paired numeric and timestamp citations matched",
        cases.len()
    );
    Ok(())
}

#[test]
fn infinite_numeric_evidence_cannot_support_an_answer() -> Result<(), Box<dyn Error>> {
    let result: Value = serde_json::from_str(r#"{"value":1e309}"#)?;
    let claim = json!({"evidence_id":1,"path":["value"],"value":3});
    assert_eq!(
        verified_numbers(
            &[claim],
            &[AnswerEvidence {
                id: 1,
                tool: "fixture",
                result: &result
            }],
            &BTreeSet::from([1]),
        ),
        Err(ClaimError::DifferentValue)
    );
    Ok(())
}
