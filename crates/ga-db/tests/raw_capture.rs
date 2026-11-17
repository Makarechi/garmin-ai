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
    activity_documents::{ActivityDocument, ApplyActivityDocument, DocumentError, DocumentStatus},
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

async fn set_collection(
    connection: &PgPool,
    id: Uuid,
    pack: &str,
    enabled: bool,
) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT INTO module_configs (id, owner_id, pack_key, tracking_enabled,
                     collection_enabled, reminders_enabled, visible, llm_enabled, settings, revision)
                 VALUES ($1, $2, $3, false, $4, false, false, false, '{}'::jsonb, 1)
                 ON CONFLICT (owner_id, pack_key) DO UPDATE SET collection_enabled = excluded.collection_enabled")
        .bind(Uuid::new_v4()).bind(id).bind(pack).bind(enabled).execute(connection).await?;
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
    let (rows, binding): (i64, bool) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM source_payloads),
             EXISTS (SELECT 1 FROM app_state WHERE key = 'account:garmin')",
    )
    .fetch_one(&inspect)
    .await?;
    assert_eq!((rows, binding), (0, false));
    let good = "a".repeat(64);
    let other = "b".repeat(64);
    sqlx::query("INSERT INTO app_state (key, value) VALUES ('account:garmin', jsonb_build_object('fingerprint', $1::text))")
        .bind(&good).execute(&inspect).await?;
    // The readiness probe may have initialized the default pack settings.
    set_collection(&inspect, owner, "training", true).await?;
    set_collection(&inspect, owner, "wellbeing", false).await?;

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
    set_collection(&inspect, owner, "wellbeing", true).await?;

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
    activity_documents(&database, &inspect, &good, archive, limits).await?;
    Ok(())
}

async fn activity_documents(
    db: &Database,
    inspect: &PgPool,
    fingerprint: &str,
    archive: Arc<Archive>,
    limits: PutLimits,
) -> Result<(), Box<dyn Error>> {
    const ID: &str = "synthetic-activity-documents";
    const ACTIVITY_START: &str = "2027-01-15T12:00:00Z";
    let start = OffsetDateTime::from(StoredInstant::from_str(ACTIVITY_START)?);
    let document = ActivityDocument::Splits;
    let endpoint = document.endpoint();
    sqlx::query(
        "INSERT INTO activities (id, kind, name, start, \"end\", timezone, details)
         VALUES ($1, 'run', 'Synthetic activity', $2, $2, 'UTC', '{}'::jsonb)",
    )
    .bind(ID)
    .bind(start)
    .execute(inspect)
    .await?;

    let capture = |body: &'static str, time: i64| {
        let archive = archive.clone();
        async move {
            db.capture_legacy_json(
                LegacyCapture {
                    authenticated_fingerprint: fingerprint,
                    endpoint,
                    source_key: ID,
                    fetched_at: at(time)?,
                    body: body.as_bytes().to_vec(),
                },
                archive,
                limits,
            )
            .await
            .map_err(Box::<dyn Error>::from)
        }
    };
    let apply = |source_id: Uuid, time: i64, timezone: &'static str| async move {
        db.apply_activity_document(ApplyActivityDocument {
            account_fingerprint: fingerprint,
            activity_id: ID,
            document,
            source_id,
            requested_at: at(time)?,
            timezone,
        })
        .await
        .map_err(Box::<dyn Error>::from)
    };
    let a = capture(r#"[{"split":4,"futureField":"α"},{"split":1}]"#, 10).await?;
    let b = capture(r#"{"split":2,"detail":[0,null]}"#, 11).await?;
    let empty = capture("[]", 12).await?;
    let older = capture(r#"{"split":3}"#, 9).await?;
    let bad = capture(r#"{"reject_synthetic_marker":true}"#, 14).await?;

    assert!(matches!(
        db.apply_activity_document(ApplyActivityDocument {
            account_fingerprint: &"c".repeat(64),
            activity_id: ID,
            document,
            source_id: a.id,
            requested_at: at(10)?,
            timezone: "UTC",
        })
        .await,
        Err(DocumentError::Access(CaptureError::Owner(
            OwnerVerificationError::Mismatch
        )))
    ));
    assert!(matches!(
        db.apply_activity_document(ApplyActivityDocument {
            account_fingerprint: fingerprint,
            activity_id: ID,
            document: ActivityDocument::Details,
            source_id: a.id,
            requested_at: at(10)?,
            timezone: "UTC",
        })
        .await,
        Err(DocumentError::SourceMissing)
    ));

    assert_eq!(
        apply(a.id, 10, "Europe/Bratislava").await?,
        DocumentStatus::Archived
    );
    assert_eq!(
        documents(inspect, ID, endpoint).await?,
        [
            (0, serde_json::json!({"split":4,"futureField":"α"})),
            (1, serde_json::json!({"split":1}))
        ]
    );
    assert_eq!(
        apply(a.id, 10, "America/Toronto").await?,
        DocumentStatus::Unchanged
    );
    let timezone: String =
        sqlx::query_scalar("SELECT value->>'timezone' FROM app_state WHERE key = $1")
            .bind(format!("ingest-meta:{}", a.id))
            .fetch_one(inspect)
            .await?;
    assert_eq!(timezone, "Europe/Bratislava");

    assert_eq!(apply(b.id, 11, "UTC").await?, DocumentStatus::Archived);
    assert_eq!(apply(empty.id, 12, "UTC").await?, DocumentStatus::Empty);
    assert_eq!(apply(older.id, 9, "UTC").await?, DocumentStatus::Stale);
    let before = documents(inspect, ID, endpoint).await?;
    assert_eq!(
        before,
        [(0, serde_json::json!({"split":2,"detail":[0,null]}))]
    );
    let state_key = format!("ingest:garmin_connect:{endpoint}:{ID}");
    let attempt = ingest_state(inspect, &state_key).await?;
    assert_eq!(attempt["source_ref"], b.id.to_string());
    assert_eq!(attempt["requested_at"], at(11)?.to_string());
    assert_eq!(
        attempt["latest_attempt"]["source_ref"],
        empty.id.to_string()
    );

    assert_eq!(apply(a.id, 13, "UTC").await?, DocumentStatus::Archived);
    let saved = documents(inspect, ID, endpoint).await?;
    assert_eq!(saved.len(), 2);
    let owner_key = format!("activity-parts-owner:{ID}:{endpoint}");
    assert_eq!(
        ingest_state(inspect, &owner_key).await?["source_ref"],
        a.id.to_string()
    );

    sqlx::query(
        "ALTER TABLE activity_parts ADD CONSTRAINT synthetic_activity_projection_failure
         CHECK (NOT (payload ? 'reject_synthetic_marker'))",
    )
    .execute(inspect)
    .await?;
    assert_eq!(apply(bad.id, 14, "UTC").await?, DocumentStatus::Error);
    assert_eq!(documents(inspect, ID, endpoint).await?, saved);
    assert_eq!(
        ingest_state(inspect, &owner_key).await?["source_ref"],
        a.id.to_string()
    );
    let attempt = ingest_state(inspect, &state_key).await?;
    assert_eq!(attempt["status"], "error");
    assert_eq!(attempt["requested_at"], at(13)?.to_string());
    assert_eq!(attempt["latest_attempt"]["source_ref"], bad.id.to_string());
    assert_eq!(
        attempt["latest_attempt"]["requested_at"],
        at(14)?.to_string()
    );
    let failure = ingest_state(inspect, &format!("ingest-meta:{}", bad.id)).await?;
    assert_eq!(failure["failed_parser_version"], 9);
    let raw_state: (String, i32, bool) = sqlx::query_as(
        "SELECT status, parser_version, payload ? 'reject_synthetic_marker'
         FROM source_payloads WHERE id = $1",
    )
    .bind(bad.id)
    .fetch_one(inspect)
    .await?;
    assert_eq!(raw_state, ("error".to_owned(), 0, true));

    sqlx::query("ALTER TABLE activity_parts DROP CONSTRAINT synthetic_activity_projection_failure")
        .execute(inspect)
        .await?;
    assert_eq!(apply(bad.id, 14, "UTC").await?, DocumentStatus::Archived);
    assert!(ingest_state(inspect, "replay:generation").await?["id"].is_string());
    let repaired = ingest_state(inspect, &format!("ingest-meta:{}", bad.id)).await?;
    assert!(repaired.get("failed_parser_version").is_none());
    assert_eq!(repaired["applied_at"], at(14)?.to_string());
    assert_eq!(documents(inspect, ID, endpoint).await?.len(), 1);
    Ok(())
}

async fn documents(
    db: &PgPool,
    activity: &str,
    endpoint: &str,
) -> Result<Vec<(i32, serde_json::Value)>, Box<dyn Error>> {
    let values: Vec<(i32, String)> = sqlx::query_as(
        "SELECT sequence, payload::text FROM activity_parts
         WHERE activity_id = $1 AND kind = $2 ORDER BY sequence",
    )
    .bind(activity)
    .bind(endpoint)
    .fetch_all(db)
    .await?;
    values
        .into_iter()
        .map(|(sequence, data)| Ok((sequence, serde_json::from_str(&data)?)))
        .collect()
}

async fn ingest_state(db: &PgPool, key: &str) -> Result<serde_json::Value, Box<dyn Error>> {
    let value: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
        .bind(key)
        .fetch_one(db)
        .await?;
    Ok(serde_json::from_str(&value)?)
}
