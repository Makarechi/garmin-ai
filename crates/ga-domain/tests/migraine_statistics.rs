use ga_domain::statistics::{match_weekday_controls, paired_mean_difference};
use serde_json::Value;
use time::{Date, format_description::well_known::Iso8601};

fn reference() -> Result<Value, serde_json::Error> {
    serde_json::from_str(include_str!("fixtures/migraine-statistics.json"))
}

fn dates(value: &Value) -> Result<Vec<Date>, Box<dyn std::error::Error>> {
    let days: Vec<String> = serde_json::from_value(value.clone())?;
    days.iter()
        .map(|day| Date::parse(day, &Iso8601::DEFAULT).map_err(Into::into))
        .collect()
}

#[test]
fn keeps_scipy_control_assignments_and_tie_order() -> Result<(), Box<dyn std::error::Error>> {
    let reference = reference()?;
    assert_eq!(reference["scipy"], "1.18.1");
    for (index, case) in reference["matching"]
        .as_array()
        .ok_or("Missing matching cases")?
        .iter()
        .enumerate()
    {
        let actual = match_weekday_controls(&dates(&case["episodes"])?, &dates(&case["controls"])?);
        let expected: Vec<(usize, usize)> = serde_json::from_value(case["pairs"].clone())?;
        assert_eq!(actual, expected, "matching fixture {index}");
    }
    Ok(())
}

#[test]
fn keeps_numpy_paired_bootstrap_and_consecutive_sign_draws()
-> Result<(), Box<dyn std::error::Error>> {
    let reference = reference()?;
    assert_eq!(reference["numpy"], "2.5.3");
    for (index, case) in reference["resampling"]
        .as_array()
        .ok_or("Missing resampling cases")?
        .iter()
        .enumerate()
    {
        let values: Vec<f64> = serde_json::from_value(case["values"].clone())?;
        let actual = paired_mean_difference(&values);
        let expected = case["p"].as_f64();
        assert_eq!(actual.sign_permutation_p, expected, "sign fixture {index}");
        match (&case["ci95"], actual.ci95) {
            (Value::Null, None) => {}
            (Value::Array(expected), Some(actual)) => {
                for (expected, actual) in expected.iter().zip(actual) {
                    let expected = expected.as_f64().ok_or("Missing interval bound")?;
                    assert!(
                        (expected - actual).abs() <= 1e-12,
                        "interval fixture {index}"
                    );
                }
            }
            _ => panic!("interval missingness differs in fixture {index}"),
        }
    }
    Ok(())
}
