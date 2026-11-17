use std::{env, error::Error, io};

use ga_db::Database;

#[tokio::test]
#[ignore = "requires the synthetic TimescaleDB fixture from pinned Python"]
async fn print_native_freshness_report() -> Result<(), Box<dyn Error>> {
    let db = Database::connect_lazy(&env::var("GA_TEST_DATABASE_URL")?)
        .map_err(|_| io::Error::other("Expected disposable TimescaleDB fixture URL"))?;
    let clock = env::var("GA_TEST_FRESHNESS_CLOCK")?.parse()?;
    let timezone = env::var("GA_TEST_FRESHNESS_ZONE")?;
    let report = db.data_freshness(clock, &timezone).await?;
    println!("freshness: {}", serde_json::to_string(&report)?);
    Ok(())
}
