use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database,
    health_analysis::{DeviceHistoryError, HealthAnalysisError},
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

#[derive(Deserialize)]
struct Case {
    name: String,
    start: String,
    end: String,
    limit: u16,
    error: Option<String>,
    expected: Option<Value>,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB with synthetic FIT evidence"]
async fn projects_only_public_fit_evidence_before_transfer() -> Result<(), Box<dyn Error>> {
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
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_DEVICE_HISTORY")?)?)?;
    for case in cases {
        let actual = db
            .device_history(case.start.parse()?, case.end.parse()?, case.limit)
            .await;
        if let Some(error) = case.error {
            assert_eq!(
                actual.err().map(|actual| actual.to_string()),
                Some(error),
                "{}",
                case.name
            );
        } else {
            let actual = actual?;
            assert!(!actual.to_string().contains("synthetic-private"));
            assert_eq!(Some(actual), case.expected, "{}", case.name);
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
    let start: StoredInstant = "2026-10-25T09:00:00Z".parse()?;
    let end: StoredInstant = "2026-10-25T10:00:00Z".parse()?;
    assert!(matches!(
        db.device_history(start, end, 100).await,
        Err(DeviceHistoryError::Analysis(
            HealthAnalysisError::ReplayPending
        ))
    ));
    Ok(())
}
