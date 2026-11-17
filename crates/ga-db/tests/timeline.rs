use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database,
    health_analysis::{HealthAnalysisError, TimelineError},
};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

#[derive(Deserialize)]
struct Case {
    start: String,
    end: String,
    name: String,
    error: Option<String>,
    expected: Option<Value>,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB timeline fixtures"]
async fn compares_layered_timelines() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected disposable TimescaleDB URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Database must end in _test"))?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(&actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_TIMELINE")?)?)?;
    for case in cases {
        let start = OffsetDateTime::parse(&case.start, &Rfc3339)?;
        let end = OffsetDateTime::parse(&case.end, &Rfc3339)?;
        let actual = database.read_owner_timeline(start, end).await;
        if let Some(expected_error) = case.error {
            assert_eq!(
                actual.err().map(|error| error.to_string()),
                Some(expected_error),
                "{}",
                case.name,
            );
        } else {
            let actual: Value = serde_json::from_slice(&actual?)?;
            let expected = case.expected.ok_or("Fixture has no expected timeline")?;
            for key in ["segments", "layers", "events", "semantics"] {
                let actual = &actual[key];
                let expected = &expected[key];
                // Python encodes a few fixed 0 or 1 fields as integers. JSON clients compare
                // their numeric values, even when the native floating-point output has a .0.
                assert!(
                    equivalent(actual, expected),
                    "{} differs at {key}: {}",
                    case.name,
                    compact_difference(actual, expected)
                );
            }
        }
    }
    sqlx::query(
        "INSERT INTO source_payloads
         (id,source,endpoint,source_key,payload_hash,archive_key,fetched_at,parser_version,status,payload)
         VALUES(gen_random_uuid(),'garmin_connect','daily','2026-10-25',repeat('a',64),
                'synthetic',clock_timestamp(),0,'pending','{}')",
    )
    .execute(&pool)
    .await?;
    let start = OffsetDateTime::parse("2026-10-25T09:00:00Z", &Rfc3339)?;
    let end = OffsetDateTime::parse("2026-10-25T10:00:00Z", &Rfc3339)?;
    assert!(matches!(
        database.read_owner_timeline(start, end).await,
        Err(TimelineError::Health(HealthAnalysisError::ReplayPending))
    ));
    Ok(())
}

fn equivalent(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(left), Value::Number(right)) if left != right => {
            left.as_f64() == right.as_f64()
        }
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| equivalent(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .all(|(key, left)| right.get(key).is_some_and(|right| equivalent(left, right)))
        }
        _ => left == right,
    }
}

fn compact_difference(left: &Value, right: &Value) -> String {
    match (left, right) {
        (Value::Array(left), Value::Array(right)) => {
            if let Some((i, (left, right))) = left
                .iter()
                .zip(right)
                .enumerate()
                .find(|(_, (l, r))| !equivalent(l, r))
            {
                format!("item {i}: {}", compact_difference(left, right))
            } else {
                format!("received {} items, expected {}", left.len(), right.len())
            }
        }
        (Value::Object(left), Value::Object(right)) => {
            if let Some((key, left)) = left
                .iter()
                .find(|(key, left)| !right.get(*key).is_some_and(|right| equivalent(left, right)))
            {
                format!(
                    "{key}: {}",
                    compact_difference(left, right.get(key).unwrap_or(&Value::Null))
                )
            } else {
                format!("received {} fields, expected {}", left.len(), right.len())
            }
        }
        _ => json!({"native": left,"pinned": right})
            .to_string()
            .chars()
            .take(256)
            .collect(),
    }
}
