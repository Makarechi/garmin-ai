use std::{env, error::Error, fs, io, str::FromStr, sync::Arc};

use ga_archive::Archive;
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    replay::{CalendarReplayOutcome, ReadReplayInput, ReplaySkip},
};
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB with source rows and a synthetic archive"]
async fn restores_activity_parts_from_verified_bytes() -> Result<(), Box<dyn Error>> {
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
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(&actual, target);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let archive_path = std::path::PathBuf::from(env::var("GA_TEST_ACTIVITY_REPLAY_ARCHIVE")?);
    let archive = Arc::new(Archive::open_read_only(&archive_path.canonicalize()?)?);
    let cases: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_ACTIVITY_REPLAY")?)?)?;
    for (name, case) in cases.as_object().ok_or("Expected fixture cases")? {
        let raw: Uuid = case["ref"]
            .as_str()
            .ok_or("Expected synthetic source id")?
            .parse()?;
        let request = ReadReplayInput {
            authenticated_fingerprint: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            source_id: raw,
            target_parser_version: 9,
            configured_timezone: "America/New_York",
            max_json_bytes: 524_288,
            max_fit_bytes: 0,
        };
        if name == "forced" {
            sqlx::raw_sql(
                "CREATE SEQUENCE synthetic_activity_replay_attempt;
                 CREATE FUNCTION fail_part_replay() RETURNS trigger LANGUAGE plpgsql AS $$
                 BEGIN
                     PERFORM nextval('synthetic_activity_replay_attempt');
                     RAISE EXCEPTION 'synthetic late document replay failure';
                 END $$;
                 CREATE TRIGGER fail_part_replay BEFORE INSERT ON activity_parts
                 FOR EACH ROW WHEN (NEW.activity_id='5') EXECUTE FUNCTION fail_part_replay();",
            )
            .execute(&inspect)
            .await?;
            assert!(
                db.replay_activity_document(request, archive.clone(), || false)
                    .await
                    .is_err()
            );
            let facts: (i32, String, bool) = sqlx::query_as(
                "SELECT parser_version,
                    (SELECT payload->>'weather' FROM activity_parts WHERE activity_id='5'),
                    (SELECT is_called FROM synthetic_activity_replay_attempt)
                 FROM source_payloads WHERE id=$1",
            )
            .bind(raw)
            .fetch_one(&inspect)
            .await?;
            assert_eq!(facts, (8, "obsolete".to_owned(), true));
            sqlx::query("DROP TRIGGER fail_part_replay ON activity_parts")
                .execute(&inspect)
                .await?;
        }
        let result = db
            .replay_activity_document(request, archive.clone(), || false)
            .await?;
        let status = match result {
            CalendarReplayOutcome::Applied(DocumentStatus::Archived) => "archived",
            CalendarReplayOutcome::Applied(DocumentStatus::Empty) => "empty",
            CalendarReplayOutcome::Applied(DocumentStatus::Unchanged) => "unchanged",
            CalendarReplayOutcome::Skipped(ReplaySkip::SupersededRevision) => "superseded_revision",
            CalendarReplayOutcome::Failed => "error",
            _ => return Err(format!("Unexpected replay for fixture {name}: {result:?}").into()),
        };
        assert_eq!(Some(status), case["status"].as_str(), "fixture {name}");
    }
    Ok(())
}
