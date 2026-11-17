use std::{env, error::Error, fs, io, num::NonZeroUsize, str::FromStr, sync::Arc, time::Duration};

use ga_archive::Archive;
use ga_db::{
    Database,
    fit_ingest::{FitIngestOutcome, FitReplayError, FitReplayOutcome},
    replay::{ReadReplayInput, ReplaySkip},
};
use ga_fit::{ExportLimits, MAX_FIT_BYTES};
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
#[ignore = "uses disposable TimescaleDB to replay synthetic FIT archives"]
async fn restores_verified_fit_parts_without_live_collection() -> Result<(), Box<dyn Error>> {
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
    let root = std::path::PathBuf::from(env::var("GA_TEST_FIT_REPLAY_ARCHIVE")?);
    let archive = Arc::new(Archive::open_read_only(&root.canonicalize()?)?);
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_FIT_REPLAY")?)?)?;
    let limits = ExportLimits {
        max_bytes: MAX_FIT_BYTES,
        max_zip_entries: NonZeroUsize::new(32).ok_or("Invalid test ZIP limit")?,
        max_duration: Duration::from_secs(10),
    };
    for case in cases {
        let id: Uuid = case.id.parse()?;
        let input = ReadReplayInput {
            authenticated_fingerprint: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            source_id: id,
            target_parser_version: 9,
            configured_timezone: "America/New_York",
            max_json_bytes: 0,
            max_fit_bytes: MAX_FIT_BYTES,
        };
        if case.name == "retained" {
            assert!(matches!(
                db.replay_fit(input, archive.clone(), limits, || true).await,
                Err(FitReplayError::Cancelled)
            ));
            assert!(matches!(
                db.replay_fit(
                    ReadReplayInput {
                        authenticated_fingerprint: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        ..input
                    },
                    archive.clone(),
                    limits,
                    || false,
                ).await,
                Err(FitReplayError::Input(_))
            ));
            let exists: bool =
                sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_state WHERE key=$1)")
                    .bind(format!("replay:{id}:9"))
                    .fetch_one(&inspect)
                    .await?;
            assert!(!exists);
        }
        if case.name == "forced" {
            sqlx::query(
                "ALTER TABLE activity_parts ADD CONSTRAINT synthetic_replay_fit_failure
                 CHECK (NOT (activity_id='7' AND kind='fit_record' AND payload->>'heart_rate'='73'))",
            )
            .execute(&inspect)
            .await?;
            assert_eq!(
                db.replay_fit(input, archive.clone(), limits, || false)
                    .await?,
                FitReplayOutcome::Applied(FitIngestOutcome::Error {
                    reason: "DatabaseError"
                })
            );
            let facts: (i32, String) = sqlx::query_as(
                "SELECT parser_version,
                    (SELECT payload->>'heart_rate' FROM activity_parts WHERE activity_id='7')
                 FROM source_payloads WHERE id=$1",
            )
            .bind(id)
            .fetch_one(&inspect)
            .await?;
            assert_eq!(facts, (8, "70".to_owned()));
            sqlx::query("ALTER TABLE activity_parts DROP CONSTRAINT synthetic_replay_fit_failure")
                .execute(&inspect)
                .await?;
        }
        let result = db
            .replay_fit(input, archive.clone(), limits, || false)
            .await?;
        let status = match result {
            FitReplayOutcome::Applied(FitIngestOutcome::Normalized { .. }) => "normalized",
            FitReplayOutcome::Applied(FitIngestOutcome::Empty) => "empty",
            FitReplayOutcome::Applied(FitIngestOutcome::Unchanged)
            | FitReplayOutcome::Skipped(ReplaySkip::Unchanged) => "unchanged",
            FitReplayOutcome::Applied(FitIngestOutcome::Error { .. })
            | FitReplayOutcome::RejectedArchive(_) => "error",
            FitReplayOutcome::Skipped(ReplaySkip::SupersededRevision) => "superseded_revision",
            _ => return Err("Unexpected synthetic FIT replay outcome".into()),
        };
        assert_eq!(status, case.status, "{}", case.name);
    }
    Ok(())
}
