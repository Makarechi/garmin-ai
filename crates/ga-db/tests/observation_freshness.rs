use std::{env, error::Error, fs, io};

use ga_db::Database;
use serde_json::{Map, Value};

type Case = (String, String, Map<String, Value>, Value);

#[tokio::test]
#[ignore = "requires the pending-free synthetic observation fixture from pinned Python"]
async fn mirrors_observed_gaps_source_health_and_calendar_recency() -> Result<(), Box<dyn Error>> {
    let db = Database::connect_lazy(&env::var("GA_TEST_DATABASE_URL")?)
        .map_err(|_| io::Error::other("Expected disposable TimescaleDB fixture URL"))?;
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_OBSERVATION_FRESHNESS")?)?)?;
    for (index, (at, zone, endpoints, expected)) in cases.into_iter().enumerate() {
        let actual = db
            .observation_freshness(at.parse()?, &zone, &endpoints)
            .await?;
        assert_same(&actual, &expected, &format!("case {index}"));
    }
    Ok(())
}

fn assert_same(actual: &Value, expected: &Value, path: &str) {
    match (actual, expected) {
        (Value::Object(actual), Value::Object(expected)) => {
            assert_eq!(
                actual.keys().collect::<Vec<_>>(),
                expected.keys().collect::<Vec<_>>(),
                "{path}"
            );
            for (field, value) in actual {
                assert_same(value, &expected[field], &format!("{path}.{field}"));
            }
        }
        (Value::Number(actual), Value::Number(expected)) => {
            assert!(
                matches!((actual.as_f64(), expected.as_f64()), (Some(a), Some(b)) if (a-b).abs() <= 1e-12),
                "{path}: {actual} vs {expected}"
            );
        }
        _ => assert_eq!(actual, expected, "{path}"),
    }
}
