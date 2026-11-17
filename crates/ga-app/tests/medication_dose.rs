use std::{env, error::Error, fs};

use ga_app::medication_dose::{
    DoseTextError, ambiguous_dose_text, normalize_dose_text, parse_literal_dose,
};
use serde_json::{Value, json};

#[test]
#[ignore = "requires the pinned Python medication text fixtures"]
fn literal_dose_text_matches_the_existing_diary() -> Result<(), Box<dyn Error>> {
    let artifact: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_MEDICATION_DOSE")?)?)?;
    let mut decisions = 0;
    for (label, run) in [
        (
            "normalize",
            normalize as fn(&str) -> Result<Value, DoseTextError>,
        ),
        ("ranges", ranges),
        ("parse", parse),
    ] {
        for case in artifact[label].as_array().ok_or("Missing fixture cases")? {
            let source = case[0].as_str().ok_or("Missing source evidence")?;
            let result = run(source).unwrap_or(Value::Null);
            assert_eq!(result, case[1], "{label}: {source:?}");
            decisions += 1;
        }
    }
    assert_eq!(
        normalize_dose_text(&"x".repeat(64_001)),
        Err(DoseTextError::InputTooLarge)
    );
    println!("{decisions} dose text and unit decisions matched pinned Python");
    Ok(())
}

fn normalize(text: &str) -> Result<Value, DoseTextError> {
    Ok(json!(normalize_dose_text(text)?))
}

fn ranges(text: &str) -> Result<Value, DoseTextError> {
    Ok(json!(ambiguous_dose_text(text)?))
}

fn parse(text: &str) -> Result<Value, DoseTextError> {
    let dose = parse_literal_dose(text)?;
    Ok(json!([dose.amount, dose.unit]))
}
