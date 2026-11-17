use std::{collections::BTreeMap, env, error::Error, fs, io, path::Path, str::FromStr, sync::Arc};

use ga_archive::Archive;
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    replay::{CalendarReplayError, CalendarReplayOutcome, ReadReplayInput, ReplaySkip},
};
use serde::Deserialize;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    refs: BTreeMap<String, String>,
    expected: BTreeMap<String, String>,
    archive: String,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and pinned Python archival cases"]
async fn rebuilds_calendar_projections_from_verified_archives() -> Result<(), Box<dyn Error>> {
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
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let cases: Fixture = serde_json::from_slice(&fs::read(env::var("GA_TEST_REPLAY_CALENDAR")?)?)?;
    let archive = Arc::new(Archive::open(Path::new(&cases.archive))?);
    let fingerprint = "a".repeat(64);

    let request = |id| ReadReplayInput {
        authenticated_fingerprint: &fingerprint,
        source_id: id,
        configured_timezone: "America/New_York",
        target_parser_version: 9,
        max_json_bytes: 512_000,
        max_fit_bytes: 100 * 1024 * 1024,
    };
    let forced = Uuid::parse_str(&cases.refs["forced"])?;
    assert!(matches!(
        db.replay_calendar(request(forced), archive.clone(), || true)
            .await,
        Err(CalendarReplayError::Cancelled)
    ));
    sqlx::query(
        "ALTER TABLE health_days ADD CONSTRAINT synthetic_replay_failure CHECK (steps <= 1000) NOT VALID",
    ).execute(&inspector).await?;
    assert!(matches!(
        db.replay_calendar(request(forced), archive.clone(), || false)
            .await,
        Err(CalendarReplayError::Publication(_))
    ));
    let state: (i32, Option<i32>, bool) = sqlx::query_as(
        "SELECT parser_version, (SELECT steps FROM health_days WHERE day='2026-10-29'),
            EXISTS (SELECT 1 FROM app_state WHERE key=concat('replay:', $1::uuid::text, ':9'))
         FROM source_payloads WHERE id=$1",
    )
    .bind(forced)
    .fetch_one(&inspector)
    .await?;
    assert_eq!(state, (8, Some(19000), false));
    sqlx::query("ALTER TABLE health_days DROP CONSTRAINT synthetic_replay_failure")
        .execute(&inspector)
        .await?;

    for name in [
        "ordinary",
        "retained",
        "active_sleep",
        "old_sleep",
        "empty",
        "damaged",
        "superseded",
        "forced",
        "tied_daily",
        "missing_interval",
    ] {
        let id = Uuid::parse_str(&cases.refs[name])?;
        let outcome = db
            .replay_calendar(request(id), archive.clone(), || false)
            .await?;
        let expected = match cases.expected[name].as_str() {
            "normalized" => CalendarReplayOutcome::Applied(DocumentStatus::Normalized),
            "empty" => CalendarReplayOutcome::Applied(DocumentStatus::Empty),
            "error" => CalendarReplayOutcome::Failed,
            "superseded_revision" => CalendarReplayOutcome::Skipped(ReplaySkip::SupersededRevision),
            _ => return Err("Unknown synthetic outcome".into()),
        };
        assert_eq!(outcome, expected, "{name}");
    }
    Ok(())
}
