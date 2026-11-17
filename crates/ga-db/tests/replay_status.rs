use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database, ReadinessError,
    health_analysis::{HealthAnalysisError, HealthPeriod},
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Date, Month};

const RETAINED: &str = "00000000-0000-4000-8000-000000001c84";

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB and pinned Python replay readiness history"]
async fn superseded_history_does_not_hide_a_retained_revision() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected PostgreSQL fixture URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    if !options
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
    {
        return Err("Use a disposable database ending in _test".into());
    }
    let inspector = PgPool::connect_with(options).await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let [ready, pending, completed]: [Value; 3] =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_REPLAY_STATUS")?)?)?;
    let day = Date::from_calendar_date(2026, Month::October, 25)?;
    let day = HealthPeriod::new(day, day)?;

    let read = || async {
        let status = db.replay_status(9).await?;
        Ok::<_, Box<dyn Error>>(json!({
            "ready": status.ready,
            "target_parser_version": status.target_parser_version,
            "jobs": status.jobs,
            "outcomes": status.outcomes,
        }))
    };
    assert_eq!(read().await?, ready);
    let baseline = db.personal_health_baseline("steps", day).await?;

    sqlx::query(
        "INSERT INTO app_state (key,value) VALUES
           ('sample-owner:synthetic-replay-status',jsonb_build_object('source_ref',$1::text))",
    )
    .bind(RETAINED)
    .execute(&inspector)
    .await?;
    assert_eq!(read().await?, pending);
    assert!(matches!(
        db.personal_health_baseline("steps", day).await,
        Err(HealthAnalysisError::ReplayPending)
    ));

    sqlx::query("UPDATE source_payloads SET parser_version=9 WHERE id=$1")
        .bind(uuid::Uuid::parse_str(RETAINED)?)
        .execute(&inspector)
        .await?;
    assert_eq!(read().await?, completed);
    assert_eq!(db.personal_health_baseline("steps", day).await?, baseline);
    assert!(!db.replay_status(8).await?.ready);

    let mut held = inspector.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *held)
        .await?;
    assert_eq!(read().await?, completed);
    assert!(matches!(
        db.personal_health_baseline("steps", day).await,
        Err(HealthAnalysisError::Storage(ReadinessError::Unavailable))
    ));
    held.rollback().await?;
    Ok(())
}
