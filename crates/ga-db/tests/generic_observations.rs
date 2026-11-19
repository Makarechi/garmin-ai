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
use ga_domain::analysis_spec::AnalysisSpec;
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
        GenericAnalysisError::Permission => "PermissionError",
        GenericAnalysisError::Health(HealthAnalysisError::ReplayPending) => "ReplayUnavailable",
        _ => "RuntimeError",
    }
}

#[tokio::test]
#[ignore = "uses dedicated synthetic observation history fixtures"]
async fn compares_generic_observation_history() -> Result<(), Box<dyn Error>> {
    let database = database().await?;
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_GENERIC_OBSERVATIONS")?)?)?;
    for case in cases {
        let spec = AnalysisSpec::from_json(&case["spec"])?;
        let result = async {
            database
                .generic_analysis_reader(
                    audience(&case).map_err(|_| GenericAnalysisError::Database)?,
                )
                .await?
                .query_observations(&spec)
                .await
        }
        .await;
        if let Some(expected) = case["error"].as_str() {
            let actual = result
                .err()
                .ok_or_else(|| format!("Expected rejection for {}", case["name"]))?;
            assert_eq!(class(&actual), expected, "{}", case["name"]);
        } else {
            let mut actual: Value = serde_json::from_reader(result?.reader())?;
            let mut expected = case["expected"].clone();
            // JSON permits integer and float spellings of the same typed double.
            // Compare their exact binary values without a numeric tolerance.
            for document in [&mut actual, &mut expected] {
                for row in document["rows"].as_array_mut().ok_or("Missing rows")? {
                    if let Some(value) = row["value"].as_f64() {
                        row["value"] = Value::from(value);
                    }
                }
            }
            assert_eq!(actual, expected, "{}", case["name"]);
        }
    }
    Ok(())
}

#[tokio::test]
#[ignore = "profiles a dedicated synthetic observation history query"]
async fn profiles_generic_observation_history() -> Result<(), Box<dyn Error>> {
    let database = database().await?;
    let case: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_GENERIC_OBSERVATION_PROFILE")?)?)?;
    let spec = AnalysisSpec::from_json(&case["spec"])?;
    let document = database
        .generic_analysis_reader(audience(&case)?)
        .await?
        .query_observations(&spec)
        .await?;
    let mut output = fs::File::create(env::var("GA_TEST_GENERIC_OBSERVATION_OUTPUT")?)?;
    io::copy(&mut document.reader(), &mut output)?;
    output.flush()?;
    eprintln!("generic observation response bytes {}", document.byte_len());
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
