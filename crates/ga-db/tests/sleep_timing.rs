use std::{env, error::Error, fs, io, str::FromStr, time::Duration};

use ga_db::{
    Database, ReadinessError,
    health_analysis::{HealthAnalysisError, sleep_timing_result_hash},
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Date, format_description::well_known::Iso8601};

#[derive(Deserialize)]
struct Reference {
    case: String,
    day: String,
    timezone: String,
    result: Value,
    hash: String,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB seeded by the pinned Python timing oracle"]
async fn verifies_sleep_while_unrelated_replay_is_locked() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL fixture URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    if !options
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
    {
        return Err("Use a disposable database ending in _test".into());
    }
    let inspector = PgPool::connect_with(options).await?;
    let mut replay = inspector.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *replay)
        .await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let cases: Vec<Reference> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_SLEEP_TIMING")?)?)?;
    for case in &cases {
        let day = Date::parse(&case.day, &Iso8601::DEFAULT)?;
        let result = tokio::time::timeout(
            Duration::from_millis(1_000),
            db.main_sleep_timing(day, &case.timezone),
        )
        .await??;
        assert_eq!(result, case.result, "{}", case.case);
        assert_eq!(
            sleep_timing_result_hash(&result)?,
            case.hash,
            "{}",
            case.case
        );
    }
    replay.rollback().await?;
    let mut maintenance = inspector.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104622)")
        .execute(&mut *maintenance)
        .await?;
    let day = Date::parse(&cases[0].day, &Iso8601::DEFAULT)?;
    assert!(matches!(
        db.main_sleep_timing(day, &cases[0].timezone).await,
        Err(HealthAnalysisError::Storage(ReadinessError::Unavailable))
    ));
    maintenance.rollback().await?;
    Ok(())
}
