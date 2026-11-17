use std::{env, error::Error, fs, io, str::FromStr, sync::Arc};

use ga_archive::Archive;
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    replay::{CalendarReplayOutcome, ReadReplayInput, ReplaySkip},
};
use serde::Deserialize;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    name: String,
    id: String,
    status: String,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB with synthetic activity archives"]
async fn restores_only_fields_still_owned_by_the_verified_activity_source()
-> Result<(), Box<dyn Error>> {
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
    let archive = std::path::PathBuf::from(env::var("GA_TEST_SUMMARY_REPLAY_ARCHIVE")?);
    let archive = Arc::new(Archive::open_read_only(&archive.canonicalize()?)?);
    let contents = fs::read(env::var("GA_TEST_SUMMARY_REPLAY")?)?;
    let cases: Vec<Case> = serde_json::from_slice(&contents)?;
    for case in cases {
        let id: Uuid = case.id.parse()?;
        let input = ReadReplayInput {
            authenticated_fingerprint: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            source_id: id,
            target_parser_version: 9,
            configured_timezone: "America/New_York",
            max_json_bytes: 524_288,
            max_fit_bytes: 0,
        };
        if case.name == "forced" {
            sqlx::raw_sql(
                "CREATE SEQUENCE synthetic_summary_replay_attempt;
                 CREATE FUNCTION fail_summary_replay() RETURNS trigger LANGUAGE plpgsql AS $$
                 BEGIN
                     PERFORM nextval('synthetic_summary_replay_attempt');
                     RAISE EXCEPTION 'synthetic late summary replay failure';
                 END $$;
                 CREATE TRIGGER fail_summary_replay BEFORE INSERT ON activities
                 FOR EACH ROW WHEN (NEW.id='11') EXECUTE FUNCTION fail_summary_replay();",
            )
            .execute(&inspect)
            .await?;
            assert!(
                db.replay_activity_summary(input, archive.clone(), || false)
                    .await
                    .is_err()
            );
            let facts: (i32, f64, f64, bool) = sqlx::query_as(
                "SELECT parser_version,
                    (SELECT calories FROM activities WHERE id='10'),
                    (SELECT calories FROM activities WHERE id='11'),
                    (SELECT is_called FROM synthetic_summary_replay_attempt)
                 FROM source_payloads WHERE id=$1",
            )
            .bind(id)
            .fetch_one(&inspect)
            .await?;
            assert_eq!(facts, (8, 0.0, 0.0, true));
            sqlx::query("DROP TRIGGER fail_summary_replay ON activities")
                .execute(&inspect)
                .await?;
        }
        let result = db
            .replay_activity_summary(input, archive.clone(), || false)
            .await?;
        let status = match result {
            CalendarReplayOutcome::Applied(DocumentStatus::Normalized) => "normalized",
            CalendarReplayOutcome::Applied(DocumentStatus::Empty) => "empty",
            CalendarReplayOutcome::Applied(DocumentStatus::Unchanged) => "unchanged",
            CalendarReplayOutcome::Applied(DocumentStatus::Error)
            | CalendarReplayOutcome::Failed => "error",
            CalendarReplayOutcome::Skipped(ReplaySkip::SupersededRevision) => "superseded_revision",
            CalendarReplayOutcome::Skipped(ReplaySkip::Unchanged) => "unchanged",
            _ => return Err("Unexpected synthetic summary replay outcome".into()),
        };
        assert_eq!(status, case.status, "{}", case.name);
    }
    Ok(())
}
