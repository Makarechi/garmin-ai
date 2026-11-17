use std::{
    env,
    error::Error,
    io::{self, Read},
    str::FromStr,
    sync::Arc,
};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    garmin_owner::OwnerVerificationError,
    raw_capture::{CaptureError, LegacyCapture},
};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

const FIRST: &str = r#"{"z":"январь","a":[1,{"extra":true}],"f":1e-6}"#;
const SAME_REORDERED: &str = r#"{ "a" : [1, {"extra":true}], "f":0.000001, "z":"январь" }"#;
const SECOND: &str = r#"{"z":"февраль","a":[1,{"extra":true}],"f":1e-6}"#;
const EXPECTED: &str = r#"{"a": [1, {"extra": true}], "f": 1e-06, "z": "январь"}"#;
const SHA: &str = "81f9d37e6db631246f2f9abda046d1a6042359349fddf6d969f45fb597a1cc34";

fn at(seconds: i64) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::from_unix_microseconds(
        1_800_000_000_000_000 + seconds * 1_000_000,
    )?)
}

fn request<'a>(fingerprint: &'a str, body: &str, fetched_at: StoredInstant) -> LegacyCapture<'a> {
    LegacyCapture {
        endpoint: "daily",
        source_key: "2027-01-15",
        authenticated_fingerprint: fingerprint,
        fetched_at,
        body: body.as_bytes().to_vec(),
    }
}

async fn add_pack(connection: &PgPool, id: Uuid, pack: &str) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT INTO module_configs (id, owner_id, pack_key, tracking_enabled,
                     collection_enabled, reminders_enabled, visible, llm_enabled, settings, revision)
                 VALUES ($1, $2, $3, false, true, false, false, false, '{}'::jsonb, 1)")
        .bind(Uuid::new_v4()).bind(id).bind(pack).execute(connection).await?;
    Ok(())
}

#[tokio::test]
#[ignore = "uses freshly migrated, disposable TimescaleDB and a temporary archive"]
async fn real_postgres_capture_preserves_pending_rows_and_checks_consent()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, name);

    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&inspect)
        .await?;
    let (rows, packs, binding): (i64, i64, bool) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM source_payloads), (SELECT count(*) FROM module_configs),
             EXISTS (SELECT 1 FROM app_state WHERE key = 'account:garmin')",
    )
    .fetch_one(&inspect)
    .await?;
    assert_eq!((rows, packs, binding), (0, 0, false));
    let good = "a".repeat(64);
    let other = "b".repeat(64);
    sqlx::query("INSERT INTO app_state (key, value) VALUES ('account:garmin', jsonb_build_object('fingerprint', $1::text))")
        .bind(&good).execute(&inspect).await?;
    add_pack(&inspect, owner, "training").await?;

    let root = tempfile::tempdir()?;
    let root_path = root.path().canonicalize()?.join("raw");
    let archive = Arc::new(Archive::open(&root_path)?);
    let limits = PutLimits {
        max_bytes: 1024,
        disk_reserve_bytes: 0,
    };
    let database = Database::connect_lazy(&url)
        .map_err(|_| io::Error::other("Invalid fixture configuration"))?;
    assert!(matches!(
        database
            .capture_legacy_json(request(&other, FIRST, at(0)?), archive.clone(), limits)
            .await,
        Err(CaptureError::Owner(OwnerVerificationError::Mismatch))
    ));
    assert!(matches!(
        database
            .capture_legacy_json(request(&good, FIRST, at(0)?), archive.clone(), limits)
            .await,
        Err(CaptureError::CollectionDisabled)
    ));
    assert_eq!(std::fs::read_dir(&root_path)?.count(), 0);
    add_pack(&inspect, owner, "wellbeing").await?;

    let too_small = PutLimits {
        max_bytes: 4,
        ..limits
    };
    assert!(matches!(
        database
            .capture_legacy_json(request(&good, FIRST, at(0)?), archive.clone(), too_small)
            .await,
        Err(CaptureError::InputTooLarge)
    ));
    let low_disk = PutLimits {
        disk_reserve_bytes: u64::MAX,
        ..limits
    };
    assert!(matches!(
        database
            .capture_legacy_json(request(&good, FIRST, at(0)?), archive.clone(), low_disk)
            .await,
        Err(CaptureError::Archive(_))
    ));
    let mut config_change = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *config_change)
        .await?;
    assert!(matches!(
        database
            .capture_legacy_json(request(&good, FIRST, at(0)?), archive.clone(), limits)
            .await,
        Err(CaptureError::Busy)
    ));
    config_change.rollback().await?;
    assert_eq!(std::fs::read_dir(&root_path)?.count(), 0);

    let first = database
        .capture_legacy_json(request(&good, FIRST, at(0)?), archive.clone(), limits)
        .await?;
    let second = database
        .capture_legacy_json(request(&good, SECOND, at(1)?), archive.clone(), limits)
        .await?;
    let repeated = database
        .capture_legacy_json(
            request(&good, SAME_REORDERED, at(2)?),
            archive.clone(),
            limits,
        )
        .await?;
    assert!(first.newly_recorded && second.newly_recorded && !repeated.newly_recorded);
    assert_eq!(first.id, repeated.id);
    assert_ne!(first.id, second.id);
    assert_eq!(first.archive_key.digest_hex(), SHA);
    let mut bytes = String::new();
    archive
        .open_reader(&first.archive_key)?
        .read_to_string(&mut bytes)?;
    assert_eq!(bytes, EXPECTED);

    let state: Vec<(Uuid, String, i32, OffsetDateTime, bool)> = sqlx::query_as(
        "SELECT id, status, parser_version, fetched_at, payload->'a'->1->>'extra' = 'true'
         FROM source_payloads ORDER BY fetched_at",
    )
    .fetch_all(&inspect)
    .await?;
    assert_eq!(
        state,
        [
            (first.id, "pending".to_owned(), 0, at(0)?.into(), true),
            (second.id, "pending".to_owned(), 0, at(1)?.into(), true),
        ]
    );
    let unmodified: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM measurements),
                 (SELECT count(*) FROM app_state WHERE key LIKE 'freshness:%' OR key LIKE 'ingest:%')",
    ).fetch_one(&inspect).await?;
    assert_eq!(unmodified, (0, 0));
    Ok(())
}
