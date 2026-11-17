use std::{env, error::Error, io, str::FromStr};

use ga_db::{Database, replay::ScheduleReplay};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime, macros::datetime};

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and prebuilt replay jobs"]
async fn repairs_and_limits_offline_replay_work() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let opts =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = opts
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(opts.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, name);
    let outcomes: Vec<(usize, bool)> =
        serde_json::from_str(&env::var("GA_TEST_REPLAY_QUEUE_OUTCOMES")?)?;
    let account = "a".repeat(64);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let base: OffsetDateTime = datetime!(2026-10-25 00:00 UTC);
    for (index, (total, incomplete)) in outcomes.into_iter().enumerate() {
        let now = StoredInstant::try_from(base + Duration::seconds(1_000 + i64::try_from(index)?))?;
        let result = db
            .schedule_replay(ScheduleReplay {
                authenticated_fingerprint: &account,
                target_parser_version: 9,
                now,
            })
            .await?;
        assert_eq!(result.queued + result.repaired, total);
        assert_eq!(result.scan_incomplete, incomplete);
        assert_eq!(result.repaired, if index == 0 { 2 } else { 0 });
    }
    let pending: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM jobs WHERE kind='raw_replay' AND status='pending'",
    )
    .fetch_one(&inspect)
    .await?;
    assert_eq!(pending, 100);
    Ok(())
}
