use std::{env, error::Error, fs};

use ga_domain::metric_contract::UserMetricContract;
use serde_json::{Value, json};

#[test]
#[ignore = "requires the pinned Python user metric contract corpus"]
fn matches_python_user_metric_contracts() -> Result<(), Box<dyn Error>> {
    let examples: Vec<(Value, Value)> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_USER_METRICS")?)?)?;
    for (index, (contract, expected)) in examples.into_iter().enumerate() {
        let output = match UserMetricContract::from_json(&contract) {
            Ok(metric) => json!([metric.key(), metric.hash(), metric.contract()]),
            Err(_) => Value::Null,
        };
        assert_eq!(output, expected, "user metric definition {index}");
    }
    Ok(())
}
