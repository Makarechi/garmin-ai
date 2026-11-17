use std::{env, error::Error, fs, io};

use ga_db::{Database, health_analysis::HealthPeriod};
use serde_json::{Value, json};
use time::{Date, format_description::well_known::Iso8601};

#[tokio::test]
#[ignore = "requires a synthetic calendar rows fixture from pinned Python"]
async fn health_reads_match_reference_including_missing_days_and_metadata()
-> Result<(), Box<dyn Error>> {
    let db = Database::connect_lazy(&env::var("GA_TEST_DATABASE_URL")?)
        .map_err(|_| io::Error::other("Expected disposable PostgreSQL fixture URL"))?;
    let cases: Vec<(String, Vec<String>, Value)> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_HEALTH_DAY_READS")?)?)?;
    for (action, dates, expected) in cases {
        let parsed: Vec<Date> = dates
            .iter()
            .map(|day| Date::parse(day, &Iso8601::DATE))
            .collect::<Result<_, _>>()?;
        let outcome = match (action.as_str(), parsed.as_slice()) {
            ("snapshot", &[day]) => db.read_health_snapshot(day).await,
            ("range", &[start, end]) => match HealthPeriod::new(start, end) {
                Ok(period) => db.read_health_range(period).await,
                Err(error) => Err(error),
            },
            _ => return Err(io::Error::other("Invalid synthetic calendar read").into()),
        };
        let output = outcome.unwrap_or_else(|error| json!({"error":error.to_string()}));
        assert_eq!(output, expected, "{action} {dates:?}");
    }
    Ok(())
}
