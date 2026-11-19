use std::{
    env,
    error::Error,
    fs,
    io::{self, Write},
    str::FromStr,
};

use ga_db::{
    Database,
    health_analysis::{
        HealthAnalysisError,
        generic::{GenericAnalysisError, GenericAudience},
    },
};
use ga_domain::analysis_spec::{AnalysisOperation, AnalysisSpec};
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

async fn database() -> Result<Database, Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected disposable PostgreSQL URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Database must end in _test")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    pool.close().await;
    Ok(Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?)
}

fn audience(case: &Value) -> Result<GenericAudience, Box<dyn Error>> {
    match case["owner"].as_str() {
        Some(owner) => Ok(GenericAudience::Mcp {
            owner: Uuid::parse_str(owner)?,
        }),
        None => Ok(GenericAudience::Owner {
            read_health: case["read_health"].as_bool().unwrap_or(true),
        }),
    }
}

fn class(error: &GenericAnalysisError) -> &'static str {
    match error {
        GenericAnalysisError::Lookup(_) => "LookupError",
        GenericAnalysisError::Value(_) => "ValueError",
        GenericAnalysisError::Data => "DataError",
        GenericAnalysisError::Encoding => "UnicodeEncodeError",
        GenericAnalysisError::InvalidSnapshot => "AttributeError",
        GenericAnalysisError::Calendar => "OverflowError",
        GenericAnalysisError::Calculation(error) => error.class_name(),
        GenericAnalysisError::Permission => "PermissionError",
        GenericAnalysisError::Health(HealthAnalysisError::ReplayPending) => "ReplayUnavailable",
        _ => "RuntimeError",
    }
}

fn compare(expected: &Value, actual: &Value, context: &str) {
    match (expected, actual) {
        (Value::Number(expected), Value::Number(actual)) => {
            let expected = expected.as_f64().unwrap_or(f64::NAN);
            let actual = actual.as_f64().unwrap_or(f64::NAN);
            assert_eq!(
                expected.to_bits(),
                actual.to_bits(),
                "{context}: {expected} versus {actual}"
            );
        }
        (Value::Object(expected), Value::Object(actual)) => {
            assert_eq!(expected.len(), actual.len(), "{context}");
            for (key, expected) in expected {
                assert!(actual.contains_key(key), "{context}.{key} is missing");
                let actual = &actual[key];
                compare(expected, actual, &format!("{context}.{key}"));
            }
        }
        (Value::Array(expected), Value::Array(actual)) => {
            assert_eq!(expected.len(), actual.len(), "{context}");
            for (index, (expected, actual)) in expected.iter().zip(actual).enumerate() {
                compare(expected, actual, &format!("{context}[{index}]"));
            }
        }
        _ => assert_eq!(expected, actual, "{context}"),
    }
}

#[tokio::test]
#[ignore = "uses dedicated synthetic threshold history fixtures"]
async fn compares_generic_threshold_history() -> Result<(), Box<dyn Error>> {
    let database = database().await?;
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_GENERIC_THRESHOLDS")?)?)?;
    for case in cases {
        let spec = AnalysisSpec::from_json(&case["spec"])
            .map_err(|error| format!("Invalid spec {} {error:?}", case["name"]))?;
        let result = async {
            let reader = database
                .generic_analysis_reader(
                    audience(&case).map_err(|_| GenericAnalysisError::Database)?,
                )
                .await?;
            match spec.operation() {
                AnalysisOperation::QueryObservations => reader.query_observations(&spec).await,
                AnalysisOperation::ThresholdSegments => reader.threshold_segments(&spec).await,
                AnalysisOperation::ThresholdByLocalHour => {
                    reader.threshold_by_local_hour(&spec).await
                }
                AnalysisOperation::QueryCompleteness => reader.query_completeness(&spec).await,
                _ => reader.aggregate_metric(&spec).await,
            }
        }
        .await;
        if let Some(expected) = case["error"].as_str() {
            let actual = result
                .err()
                .ok_or_else(|| format!("Expected rejection for {}", case["name"]))?;
            assert_eq!(class(&actual), expected, "{}", case["name"]);
            if let Some(message) = case["error_message"].as_str() {
                assert_eq!(actual.to_string(), message, "{}", case["name"]);
            }
        } else {
            let actual: Value = serde_json::from_reader(result?.reader())?;
            compare(
                &case["expected"],
                &actual,
                case["name"].as_str().unwrap_or("case"),
            );
        }
    }
    Ok(())
}

#[tokio::test]
#[ignore = "profiles a dedicated synthetic threshold history query"]
async fn profiles_generic_threshold_history() -> Result<(), Box<dyn Error>> {
    let database = database().await?;
    let case: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_GENERIC_THRESHOLD_PROFILE")?)?)?;
    let spec = AnalysisSpec::from_json(&case["spec"])
        .map_err(|error| format!("Invalid spec {} {error:?}", case["name"]))?;
    let reader = database.generic_analysis_reader(audience(&case)?).await?;
    let document = match spec.operation() {
        AnalysisOperation::ThresholdSegments => reader.threshold_segments(&spec).await?,
        AnalysisOperation::ThresholdByLocalHour => reader.threshold_by_local_hour(&spec).await?,
        AnalysisOperation::QueryCompleteness => reader.query_completeness(&spec).await?,
        _ => reader.aggregate_metric(&spec).await?,
    };
    let mut output = fs::File::create(env::var("GA_TEST_GENERIC_THRESHOLD_OUTPUT")?)?;
    io::copy(&mut document.reader(), &mut output)?;
    output.flush()?;
    eprintln!("generic threshold response bytes {}", document.byte_len());
    if let Ok(status) = fs::read_to_string("/proc/self/status") {
        for line in status
            .lines()
            .filter(|line| line.starts_with("VmHWM:") || line.starts_with("VmRSS:"))
        {
            eprintln!("{line}");
        }
        for name in ["memory.peak", "memory.swap.peak", "memory.events"] {
            eprintln!(
                "{name}\n{}",
                fs::read_to_string(format!("/sys/fs/cgroup/{name}"))?
            );
        }
    }
    Ok(())
}
