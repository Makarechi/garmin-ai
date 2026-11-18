use std::{env, error::Error};

use ga_db::Database;
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

#[tokio::test]
#[ignore = "profiles the full 2000-run result on a disposable synthetic TimescaleDB"]
async fn reads_the_maximum_running_analysis_with_bounded_memory() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options = format!("postgresql://{address}")
        .parse::<PgConnectOptions>()?
        .disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected test database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let start: StoredInstant = "2026-10-25T09:00:00Z".parse()?;
    let end: StoredInstant = "2026-10-26T09:00:00Z".parse()?;
    let document = database
        .verified_health_reader()
        .await?
        .running_efficiency(start, end, 0.0, 250.0)
        .await?;
    assert_eq!(document["n"], 2000);
    assert_eq!(document["excluded"], 0);
    let rows = document["rows"].as_array().ok_or("Missing running rows")?;
    assert_eq!(rows.len(), 2000);
    assert_eq!(rows[0]["readiness"].as_f64(), Some(90.0));
    assert_eq!(rows[1999]["readiness"].as_f64(), Some(90.0));
    assert_eq!(document["comparison_groups"][0]["n"], 2000);
    let bytes = serde_json::to_vec(&document)?;
    assert!(bytes.len() > 1_000_000);
    println!(
        "Verified all 2000 chronological runs and serialized {} bytes of sourced evidence",
        bytes.len()
    );
    Ok(())
}
