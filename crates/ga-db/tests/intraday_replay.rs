use std::{
    collections::BTreeMap, env, error::Error, fs, io, path::PathBuf, str::FromStr, sync::Arc,
};

use ga_archive::Archive;
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    replay::{CalendarReplayError, CalendarReplayOutcome, ReadReplayInput},
};
use serde::Deserialize;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    refs: BTreeMap<String, String>,
    expected: BTreeMap<String, String>,
    archive: PathBuf,
}

#[tokio::test]
#[ignore = "requires disposable TimescaleDB and verified synthetic archive journal"]
async fn recovers_only_verified_intraday_fallbacks() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let host = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected test URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{host}"))?.disable_statement_logging();
    if !options
        .get_database()
        .is_some_and(|database| database.ends_with("_test"))
    {
        return Err("Use a disposable database ending in _test".into());
    }
    let inspect = PgPool::connect_with(options).await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Expected fixture configuration")?;
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_INTRADAY_REPLAY")?)?)?;
    let archive = Arc::new(Archive::open_read_only(&fixture.archive)?);
    let fingerprint = "a".repeat(64);
    let request = |id| ReadReplayInput {
        authenticated_fingerprint: &fingerprint,
        source_id: id,
        target_parser_version: 9,
        configured_timezone: "America/New_York",
        max_json_bytes: 1_048_576,
        max_fit_bytes: 1024 * 1024,
    };
    let fallback = Uuid::parse_str(&fixture.refs["fallback"])?;
    assert!(matches!(
        database
            .replay_intraday(request(fallback), Arc::clone(&archive), || true)
            .await,
        Err(CalendarReplayError::Cancelled)
    ));
    sqlx::query("ALTER TABLE measurements ADD CONSTRAINT synthetic_replay_staging_failure CHECK (value != 70) NOT VALID")
        .execute(&inspect)
        .await?;
    assert!(matches!(
        database
            .replay_intraday(request(fallback), Arc::clone(&archive), || false)
            .await,
        Err(CalendarReplayError::Publication(_))
    ));
    let value: f64 = sqlx::query_scalar(
        "SELECT value FROM measurements WHERE ts='2026-10-18T00:01:00+00:00' AND metric='heart_rate_bpm'"
    ).fetch_one(&inspect).await?;
    assert_eq!(value, 151.0);
    sqlx::query("ALTER TABLE measurements DROP CONSTRAINT synthetic_replay_staging_failure")
        .execute(&inspect)
        .await?;

    for name in ["fallback", "retained", "unknown", "corrupt", "a_b_a"] {
        let id = Uuid::parse_str(&fixture.refs[name])?;
        let expected = match fixture.expected[name].as_str() {
            "normalized" => CalendarReplayOutcome::Applied(DocumentStatus::Normalized),
            "error" => CalendarReplayOutcome::Applied(DocumentStatus::Error),
            _ => return Err("unknown fixture status".into()),
        };
        let result = database
            .replay_intraday(request(id), Arc::clone(&archive), || false)
            .await?;
        assert_eq!(result, expected, "{name}");
    }
    Ok(())
}
