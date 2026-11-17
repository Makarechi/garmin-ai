use std::{
    env,
    error::Error,
    io::{self, Cursor, Read},
    num::NonZeroUsize,
    str::FromStr,
    sync::Arc,
    time::Duration,
};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    fit_ingest::{FitIngestError, FitIngestOutcome, LegacyFitCapture},
    garmin_owner::OwnerVerificationError,
    raw_capture::CaptureError,
};
use ga_domain::timestamp::StoredInstant;
use ga_fit::{ExportLimits, MAX_FIT_BYTES};
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

const FIRST: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\xb0\xe5";
const NEXT: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x49\x71\x25";
const ACTIVITY: &str = "synthetic-legacy-fit-publication";

fn at(seconds: i64) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::from_unix_microseconds(
        1_800_000_000_000_000 + seconds * 1_000_000,
    )?)
}

async fn visible(db: &PgPool) -> Result<(String, Value, Vec<i32>), Box<dyn Error>> {
    let (key, details): (String, String) =
        sqlx::query_as("SELECT fit_key, details::text FROM activities WHERE id = $1")
            .bind(ACTIVITY)
            .fetch_one(db)
            .await?;
    let rates = sqlx::query_scalar(
        "SELECT (payload->>'heart_rate')::int FROM activity_parts
         WHERE activity_id = $1 AND kind = 'fit_record' ORDER BY sequence",
    )
    .bind(ACTIVITY)
    .fetch_all(db)
    .await?;
    Ok((key, serde_json::from_str(&details)?, rates))
}

async fn version(db: &PgPool) -> Result<Value, Box<dyn Error>> {
    let state: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
        .bind(format!("fit-version:{ACTIVITY}"))
        .fetch_one(db)
        .await?;
    Ok(serde_json::from_str(&state)?)
}

#[tokio::test]
#[ignore = "uses a freshly migrated disposable TimescaleDB and archive"]
async fn real_postgres_late_failures_preserve_the_last_verified_fit() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, expected);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let existing: i64 =
        sqlx::query_scalar("SELECT count(*) FROM activity_parts WHERE activity_id = $1")
            .bind(ACTIVITY)
            .fetch_one(&inspect)
            .await?;
    assert_eq!(existing, 0);

    let person: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&inspect)
        .await?;
    let fingerprint = "a".repeat(64);
    sqlx::query(
        "INSERT INTO app_state (key, value) VALUES ('account:garmin', jsonb_build_object('fingerprint', $1::text))
         ON CONFLICT (key) DO UPDATE SET value = excluded.value",
    )
    .bind(&fingerprint).execute(&inspect).await?;
    sqlx::query(
        "INSERT INTO module_configs (id, owner_id, pack_key, tracking_enabled,
            collection_enabled, reminders_enabled, visible, llm_enabled, settings, revision)
         VALUES ($1, $2, 'training', false, true, false, false, false, '{}'::jsonb, 1)
         ON CONFLICT (owner_id, pack_key) DO UPDATE SET collection_enabled = true",
    )
    .bind(Uuid::new_v4())
    .bind(person)
    .execute(&inspect)
    .await?;
    sqlx::query(
        "INSERT INTO activities (id, kind, name, start, \"end\", timezone, details)
         VALUES ($1, 'run', 'Synthetic FIT publication', $2, $2, 'UTC', '{\"provider\":\"synthetic\"}'::jsonb)",
    )
    .bind(ACTIVITY).bind(OffsetDateTime::from(at(0)?)).execute(&inspect).await?;
    sqlx::query(
        "INSERT INTO activity_parts (activity_id, kind, sequence, payload)
         VALUES ($1, 'activity_weather', 0, '{\"weather\":\"synthetic\"}'::jsonb)",
    )
    .bind(ACTIVITY)
    .execute(&inspect)
    .await?;

    let root = tempfile::tempdir()?;
    let archive_path = root.path().canonicalize()?.join("raw");
    let archive = Arc::new(Archive::open(&archive_path)?);
    let write_limits = PutLimits {
        max_bytes: MAX_FIT_BYTES,
        disk_reserve_bytes: 0,
    };
    let read_limits = ExportLimits {
        max_bytes: MAX_FIT_BYTES,
        max_zip_entries: NonZeroUsize::MIN,
        max_duration: Duration::from_secs(10),
    };
    assert!(matches!(
        db.capture_legacy_activity_export(
            LegacyFitCapture {
                activity_id: ACTIVITY,
                authenticated_fingerprint: &"b".repeat(64),
                fetched_at: at(0)?
            },
            Cursor::new(FIRST),
            archive.clone(),
            write_limits,
        )
        .await,
        Err(FitIngestError::Access(CaptureError::Owner(
            OwnerVerificationError::Mismatch
        )))
    ));
    assert_eq!(std::fs::read_dir(&archive_path)?.count(), 0);

    let capture = |data: Vec<u8>, when: i64| {
        let fingerprint = &fingerprint;
        let archive = archive.clone();
        let db = &db;
        async move {
            db.capture_legacy_activity_export(
                LegacyFitCapture {
                    activity_id: ACTIVITY,
                    authenticated_fingerprint: fingerprint,
                    fetched_at: at(when)?,
                },
                Cursor::new(data),
                archive,
                write_limits,
            )
            .await
            .map_err(Box::<dyn Error>::from)
        }
    };

    let first = capture(FIRST.to_vec(), 0).await?;
    let mut original = Vec::new();
    archive
        .open_verified_reader(first.key(), MAX_FIT_BYTES, || false)?
        .read_to_end(&mut original)?;
    assert_eq!(original, FIRST);
    assert_eq!(first.key().digest_hex(), ga_domain::sha256_hex(FIRST));
    let first_outcome = db
        .apply_legacy_activity_export(&first, archive.clone(), read_limits)
        .await?;
    assert_eq!(first_outcome, FitIngestOutcome::Normalized { rows: 1 });
    assert_eq!(visible(&inspect).await?.2, [72]);

    let mut corrupt = NEXT.repeat(515);
    let footer = corrupt.len() - 1;
    corrupt[footer] ^= 1;
    let bad = capture(corrupt, 3).await?;
    assert_eq!(
        db.apply_legacy_activity_export(&bad, archive.clone(), read_limits)
            .await?,
        FitIngestOutcome::Error {
            reason: "FitCRCError"
        },
    );
    let (key, details, rates) = visible(&inspect).await?;
    assert_eq!(key, first.key().to_string());
    assert_eq!(rates, [72]);
    assert_eq!(details["parsed_fit_key"], key);
    assert_eq!(
        details["fit_attempt_source_ref"],
        bad.source_id().to_string()
    );
    assert_eq!(
        version(&inspect).await?["source_ref"],
        first.source_id().to_string()
    );
    assert_eq!(
        version(&inspect).await?["latest_attempt"]["source_ref"],
        bad.source_id().to_string()
    );

    let older = capture(NEXT.to_vec(), 2).await?;
    assert_eq!(
        db.apply_legacy_activity_export(&older, archive.clone(), read_limits)
            .await?,
        FitIngestOutcome::Stale,
    );
    let next = capture(NEXT.to_vec(), 4).await?;
    assert_eq!(next.source_id(), older.source_id());
    sqlx::query(
        "ALTER TABLE activity_parts ADD CONSTRAINT synthetic_fit_publication_failure
         CHECK (NOT (kind = 'fit_record' AND payload->>'heart_rate' = '73'))",
    )
    .execute(&inspect)
    .await?;
    assert_eq!(
        db.apply_legacy_activity_export(&next, archive.clone(), read_limits)
            .await?,
        FitIngestOutcome::Error {
            reason: "DatabaseError"
        },
    );
    assert_eq!(visible(&inspect).await?.2, [72]);
    sqlx::query("ALTER TABLE activity_parts DROP CONSTRAINT synthetic_fit_publication_failure")
        .execute(&inspect)
        .await?;
    sqlx::query("UPDATE module_configs SET collection_enabled = false WHERE owner_id = $1 AND pack_key = 'training'")
        .bind(person).execute(&inspect).await?;
    assert!(matches!(
        db.apply_legacy_activity_export(&next, archive.clone(), read_limits)
            .await,
        Err(FitIngestError::Access(CaptureError::CollectionDisabled)),
    ));
    assert_eq!(visible(&inspect).await?.2, [72]);
    sqlx::query("UPDATE module_configs SET collection_enabled = true WHERE owner_id = $1 AND pack_key = 'training'")
        .bind(person).execute(&inspect).await?;
    assert_eq!(
        db.apply_legacy_activity_export(&next, archive.clone(), read_limits)
            .await?,
        FitIngestOutcome::Normalized { rows: 1 },
    );
    let (key, details, rates) = visible(&inspect).await?;
    assert_eq!(key, next.key().to_string());
    assert_eq!(details["fit_status"], "normalized");
    assert!(details.get("fit_error_type").is_none());
    assert_eq!(rates, [73]);
    assert_eq!(
        db.apply_legacy_activity_export(&next, archive.clone(), read_limits)
            .await?,
        FitIngestOutcome::Unchanged,
    );
    let empty = capture(Vec::new(), 5).await?;
    assert_eq!(
        db.apply_legacy_activity_export(&empty, archive.clone(), read_limits)
            .await?,
        FitIngestOutcome::Empty,
    );
    assert_eq!(visible(&inspect).await?.2, [73]);
    let weather: String = sqlx::query_scalar(
        "SELECT payload->>'weather' FROM activity_parts WHERE activity_id = $1 AND kind = 'activity_weather'",
    )
    .bind(ACTIVITY).fetch_one(&inspect).await?;
    assert_eq!(weather, "synthetic");

    let raw: (String, i32) =
        sqlx::query_as("SELECT status, parser_version FROM source_payloads WHERE id = $1")
            .bind(bad.source_id())
            .fetch_one(&inspect)
            .await?;
    assert_eq!(raw, ("error".to_owned(), 0));
    assert!(
        archive
            .open_verified_reader(bad.key(), MAX_FIT_BYTES, || false)
            .is_ok()
    );
    Ok(())
}
