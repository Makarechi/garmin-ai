use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    health_analysis::{HealthAnalysisError, NapPolicy},
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::Date;

#[derive(Deserialize)]
struct Case {
    start: String,
    end: String,
    zone: String,
    policy: String,
    value: Value,
}

#[tokio::test]
#[ignore = "reads the same synthetic rows as pinned Python on disposable TimescaleDB"]
async fn sleep_analysis_matches_main_and_nap_evidence() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(current, expected);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_SLEEP_ANALYSIS")?)?)?;
    for (index, case) in cases.iter().enumerate() {
        let policy = match case.policy.as_str() {
            "separate" => NapPolicy::Separate,
            "include_confirmed" => NapPolicy::IncludeConfirmed,
            _ => return Err("Unknown reference nap policy".into()),
        };
        let mut actual = match database
            .sleep_analysis(day(&case.start)?, day(&case.end)?, &case.zone, policy)
            .await
        {
            Ok(value) => value,
            Err(HealthAnalysisError::SleepRange) => Value::String("invalid".into()),
            Err(error) => return Err(error.into()),
        };
        let mut expected = case.value.clone();
        if let Some(summary) = actual.get_mut("summary").and_then(Value::as_object_mut)
            && let Some(reference) = expected.get_mut("summary").and_then(Value::as_object_mut)
        {
            for name in [
                "bedtime_circular_sd_minutes",
                "wake_time_circular_sd_minutes",
            ] {
                let left = summary
                    .remove(name)
                    .ok_or("Missing native circular spread")?;
                let right = reference
                    .remove(name)
                    .ok_or("Missing baseline circular spread")?;
                match (left.as_f64(), right.as_f64()) {
                    (Some(x), Some(y)) => {
                        assert!((x - y).abs() < 1e-8, "{index} {name}: {x} / {y}");
                    }
                    _ => assert_eq!(left, right, "{index} {name}"),
                }
            }
        }
        compare(&actual, &expected, &index.to_string());
    }
    println!(
        "{} main sleep and nap analysis decisions matched pinned Python",
        cases.len()
    );
    Ok(())
}

fn day(text: &str) -> Result<Date, Box<dyn Error>> {
    Ok(Date::parse(
        text,
        &time::format_description::parse_borrowed::<2>("[year]-[month]-[day]")?,
    )?)
}

fn compare(actual: &Value, expected: &Value, path: &str) {
    match (actual, expected) {
        (Value::Object(a), Value::Object(b)) => {
            assert_eq!(a.len(), b.len(), "{path}");
            for (key, value) in b {
                compare(&a[key], value, &format!("{path}/{key}"));
            }
        }
        (Value::Array(a), Value::Array(b)) => {
            assert_eq!(a.len(), b.len(), "{path}");
            for (index, (a, b)) in a.iter().zip(b).enumerate() {
                compare(a, b, &format!("{path}/{index}"));
            }
        }
        (Value::Number(a), Value::Number(b)) => assert_eq!(a.as_f64(), b.as_f64(), "{path}"),
        _ => assert_eq!(actual, expected, "{path}"),
    }
}
