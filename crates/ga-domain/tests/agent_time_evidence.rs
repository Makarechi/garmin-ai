use std::{env, error::Error, fs};

use ga_domain::{
    agent_interpretation::{AgentInterpretation, TimeClarification},
    timestamp::StoredInstant,
};
use serde_json::Value;

#[test]
#[ignore = "compares against the pinned Python interpretation fixture"]
fn only_accepts_local_times_supported_by_the_exact_message() -> Result<(), Box<dyn Error>> {
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_AGENT_TIME_EVIDENCE")?)?)?;
    let now: StoredInstant = fixture["now"]
        .as_str()
        .ok_or("Missing reference clock")?
        .parse()?;
    let cases = fixture["cases"]
        .as_array()
        .ok_or("Missing time evidence cases")?;
    for case in cases {
        let name = case["name"].as_str().ok_or("Missing case name")?;
        let response = AgentInterpretation::from_value(&case["command"])?;
        let text = case["text"]
            .as_str()
            .ok_or("Missing authenticated message")?;
        let target = case["target_timezone"].as_str();
        let expected = match case["reason"].as_str() {
            None => Ok(()),
            Some("timezone_mismatch") => Err(TimeClarification::TimezoneMismatch),
            Some("ambiguous_time") => Err(TimeClarification::AmbiguousTime),
            Some("future_time") => Err(TimeClarification::FutureTime),
            Some(_) => return Err("Unexpected reference time response".into()),
        };
        assert_eq!(
            response.verify_time_evidence(text, now, target),
            expected,
            "{name}"
        );
        if target.is_some() && name != "explicit_timezone_change" {
            assert_eq!(
                response.verify_time_evidence(text, now, None),
                Err(TimeClarification::MissingTargetTimezone),
                "{name}",
            );
        }
    }
    println!("{} paired local time decisions matched", cases.len());
    Ok(())
}
