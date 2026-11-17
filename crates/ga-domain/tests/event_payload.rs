use std::{
    error::Error,
    fs::File,
    io::{self, BufRead, BufReader},
};

use ga_domain::event_payload::{InvalidPayload, SystemPayload};
use serde_json::{Value, json};

#[test]
fn unknown_medication_fields_remain_null_in_the_payload() -> Result<(), InvalidPayload> {
    let payload = SystemPayload::from_json(br#"{"type":"medication"}"#, 1024)?;
    assert_eq!(payload.kind(), "medication");
    assert_eq!(
        payload.json(),
        &json!({
            "type": "medication",
            "name": null,
            "dose": null,
            "unit": null,
            "reason_event_id": null,
        })
    );
    Ok(())
}

#[test]
fn dose_range_and_explicit_observations_reject_invalid_values() {
    let caffeine = json!({"type":"caffeine", "beverage":"synthetic", "caffeine_mg_min":10, "caffeine_mg_estimate":9});
    assert!(matches!(
        SystemPayload::from_value(caffeine),
        Err(InvalidPayload::DoseRange)
    ));
    assert!(matches!(
        SystemPayload::from_value(json!({"type":"wellbeing_observation"})),
        Err(InvalidPayload::EmptyObservation)
    ));
    assert!(matches!(
        SystemPayload::from_value(json!({"type":"wellbeing_observation","energy":true})),
        Err(InvalidPayload::InvalidField("energy"))
    ));
    assert!(matches!(
        SystemPayload::from_json(br#"{"type":"migraine"}"#, 2),
        Err(InvalidPayload::InputTooLarge)
    ));
}

#[test]
#[ignore = "generate the synthetic Python reference with migration/check_system_payloads.py"]
fn compare_against_the_pinned_python_payloads() -> Result<(), Box<dyn Error>> {
    let path = std::env::var_os("GA_TEST_SYSTEM_PAYLOADS")
        .ok_or_else(|| io::Error::other("Use the generated synthetic reference file"))?;
    let mut count = 0;
    let mut mismatches = Vec::new();
    for (line_number, line) in BufReader::new(File::open(path)?).lines().enumerate() {
        let case: Value = serde_json::from_str(&line?)?;
        let input = case["input"]
            .as_str()
            .ok_or_else(|| io::Error::other("Missing synthetic input"))?;
        let actual = SystemPayload::from_json(input.as_bytes(), 65_536);
        let actual = actual.as_ref().map_or(&Value::Null, SystemPayload::json);
        if actual != &case["expected"] {
            mismatches.push((line_number + 1, input.chars().take(180).collect::<String>()));
        }
        count += 1;
    }
    assert!(count > 1000);
    assert!(mismatches.is_empty(), "{mismatches:?}");
    Ok(())
}
