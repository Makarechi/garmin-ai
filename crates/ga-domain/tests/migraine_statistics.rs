use ga_domain::statistics::{match_weekday_controls, paired_mean_difference};
use serde_json::Value;
use time::{Date, format_description::well_known::Iso8601};

fn reference() -> Value {
    serde_json::from_str(include_str!("fixtures/migraine-statistics.json")).unwrap()
}

fn dates(value: &Value) -> Vec<Date> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|day| Date::parse(day.as_str().unwrap(), &Iso8601::DEFAULT).unwrap())
        .collect()
}

#[test]
fn keeps_scipy_control_assignments_and_tie_order() {
    let reference = reference();
    assert_eq!(reference["scipy"], "1.18.1");
    for (index, case) in reference["matching"].as_array().unwrap().iter().enumerate() {
        let actual = match_weekday_controls(&dates(&case["episodes"]), &dates(&case["controls"]));
        let expected: Vec<(usize, usize)> = case["pairs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|pair| {
                (
                    pair[0].as_u64().unwrap() as usize,
                    pair[1].as_u64().unwrap() as usize,
                )
            })
            .collect();
        assert_eq!(actual, expected, "matching fixture {index}");
    }
}

#[test]
fn keeps_numpy_paired_bootstrap_and_consecutive_sign_draws() {
    let reference = reference();
    assert_eq!(reference["numpy"], "2.5.3");
    for (index, case) in reference["resampling"]
        .as_array()
        .unwrap()
        .iter()
        .enumerate()
    {
        let values: Vec<_> = case["values"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_f64().unwrap())
            .collect();
        let actual = paired_mean_difference(&values);
        let expected = case["p"].as_f64();
        assert_eq!(actual.sign_permutation_p, expected, "sign fixture {index}");
        match (&case["ci95"], actual.ci95) {
            (Value::Null, None) => {}
            (Value::Array(expected), Some(actual)) => {
                for (expected, actual) in expected.iter().zip(actual) {
                    let expected = expected.as_f64().unwrap();
                    assert!(
                        (expected - actual).abs() <= 1e-12,
                        "interval fixture {index}"
                    );
                }
            }
            _ => panic!("interval missingness differs in fixture {index}"),
        }
    }
}
