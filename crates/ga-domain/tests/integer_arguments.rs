use ga_domain::legacy_json::integer_argument;
use serde_json::Value;

#[test]
fn keeps_legacy_typed_integer_coercion_and_large_values() -> Result<(), serde_json::Error> {
    let cases: Vec<(Value, Option<String>)> =
        serde_json::from_str(include_str!("fixtures/integer-arguments.json"))?;
    for (index, (value, expected)) in cases.into_iter().enumerate() {
        let actual = integer_argument(&value).map(|value| value.to_string());
        assert_eq!(actual, expected, "integer argument fixture {index}");
    }
    Ok(())
}
