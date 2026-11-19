use ga_domain::legacy_json::{Utf8Layout, encode, multiply_numbers, sum_numbers};
use serde_json::Value;

#[test]
fn keeps_python_numeric_types_and_compensated_evidence_sums()
-> Result<(), Box<dyn std::error::Error>> {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/numeric-evidence.json"))?;
    for (index, case) in cases.into_iter().enumerate() {
        let values = case["values"].as_array().ok_or("Missing numeric inputs")?;
        let result = if case["operation"] == "sum" {
            sum_numbers(values)
        } else {
            multiply_numbers(&values[0], &values[1])
        };
        let actual = match result {
            Err(_) => None,
            Ok(value) => {
                let input = serde_json::to_vec(&value)?;
                Some(String::from_utf8(encode(
                    &input,
                    Utf8Layout::Ordered,
                    input.len(),
                )?)?)
            }
        };
        let expected = case["result"].as_str();
        assert_eq!(
            actual.as_deref(),
            expected,
            "numeric fixture {index}: {values:?}"
        );
    }
    Ok(())
}
