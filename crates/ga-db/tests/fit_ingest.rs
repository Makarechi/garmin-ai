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
    jobs::{FailureReason, GarminJobLease, JobStoreError},
    raw_capture::CaptureError,
};
use ga_domain::timestamp::StoredInstant;
use ga_fit::{ExportLimits, MAX_FIT_BYTES};
use serde_json::{Value, json};
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
    verify_atomic_queue_effects(
        &db,
        &inspect,
        archive,
        &fingerprint,
        write_limits,
        read_limits,
    )
    .await?;
    Ok(())
}

fn clock(now: OffsetDateTime) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::from_unix_microseconds(i64::try_from(
        now.unix_timestamp_nanos() / 1_000,
    )?)?)
}

async fn claim(
    db: &Database,
    inspect: &PgPool,
    account: &str,
    ttl: u32,
) -> Result<GarminJobLease, Box<dyn Error>> {
    let id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO jobs (id, kind, payload, dedup_key, run_at, status, attempts)
         VALUES ($1, 'garmin_fit', $2::jsonb, $3, clock_timestamp(), 'pending', 0)",
    )
    .bind(id)
    .bind(json!({"activity_id": ACTIVITY, "account": account}).to_string())
    .bind(format!("fit:synthetic:{id}"))
    .execute(inspect)
    .await?;
    let now = OffsetDateTime::now_utc();
    let lease = db
        .claim_garmin_job(clock(now)?, now.date(), ttl, false)
        .await?
        .ok_or_else(|| io::Error::other("Synthetic FIT job was not claimed"))?;
    assert_eq!(lease.id(), id);
    Ok(lease)
}

async fn freshness(inspect: &PgPool) -> Result<Option<Value>, Box<dyn Error>> {
    let raw: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
            .bind(format!("freshness:activity_fit:{ACTIVITY}"))
            .fetch_optional(inspect)
            .await?;
    Ok(raw.map(|raw| serde_json::from_str(&raw)).transpose()?)
}

async fn job_status(
    inspect: &PgPool,
    id: Uuid,
) -> Result<(String, i32, Option<String>), sqlx::Error> {
    sqlx::query_as("SELECT status, attempts, last_error FROM jobs WHERE id = $1")
        .bind(id)
        .fetch_one(inspect)
        .await
}

async fn verify_atomic_queue_effects(
    db: &Database,
    inspect: &PgPool,
    archive: Arc<Archive>,
    fingerprint: &str,
    put_limits: PutLimits,
    limits: ExportLimits,
) -> Result<(), Box<dyn Error>> {
    let request = |fetched_at| LegacyFitCapture {
        activity_id: ACTIVITY,
        authenticated_fingerprint: fingerprint,
        fetched_at,
    };
    let expiring = claim(db, inspect, fingerprint, 60).await?;
    assert!(matches!(
        db.authorize_legacy_fit_job(
            &LegacyFitCapture {
                activity_id: "synthetic-wrong-activity",
                ..request(at(6)?)
            },
            &expiring
        )
        .await,
        Err(FitIngestError::WrongJob)
    ));
    db.authorize_legacy_fit_job(&request(at(6)?), &expiring)
        .await?;

    // Publication renews the initial lease. Expire the actual lease from
    // inside the replacement to exercise the final commit fence.
    sqlx::raw_sql(
        "CREATE SEQUENCE synthetic_fit_started;
         CREATE TABLE synthetic_fit_lease (id uuid PRIMARY KEY);
         CREATE FUNCTION expire_fit_during_replacement() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN
            PERFORM nextval('synthetic_fit_started');
            UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'
             WHERE id=(SELECT id FROM synthetic_fit_lease);
            RETURN NEW;
         END $$;
         CREATE TRIGGER synthetic_fit_expires BEFORE INSERT ON activity_parts
           FOR EACH ROW WHEN (NEW.kind = 'fit_record') EXECUTE FUNCTION expire_fit_during_replacement();",
    )
    .execute(inspect)
    .await?;
    sqlx::query("INSERT INTO synthetic_fit_lease (id) VALUES ($1)")
        .bind(expiring.id())
        .execute(inspect)
        .await?;
    let result = db
        .ingest_leased_legacy_fit(
            request(at(6)?),
            &expiring,
            Cursor::new(FIRST),
            archive.clone(),
            put_limits,
            limits,
        )
        .await;
    assert!(
        matches!(result, Err(FitIngestError::Lease(JobStoreError::LeaseLost))),
        "{result:?}"
    );
    let began: bool = sqlx::query_scalar("SELECT is_called FROM synthetic_fit_started")
        .fetch_one(inspect)
        .await?;
    assert!(began);
    assert_eq!(visible(inspect).await?.2, [73]);
    assert_eq!(freshness(inspect).await?, None);
    assert_eq!(
        job_status(inspect, expiring.id()).await?,
        ("running".to_owned(), 1, None)
    );
    sqlx::raw_sql("DROP TRIGGER synthetic_fit_expires ON activity_parts; DROP FUNCTION expire_fit_during_replacement(); DROP SEQUENCE synthetic_fit_started; DROP TABLE synthetic_fit_lease;")
        .execute(inspect)
        .await?;
    let expired = sqlx::query(
        "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'
         WHERE id=$1 AND status='running' AND lease_until>clock_timestamp()",
    )
    .bind(expiring.id())
    .execute(inspect)
    .await?;
    assert_eq!(expired.rows_affected(), 1);

    let now = OffsetDateTime::now_utc();
    let recovered = db
        .claim_garmin_job(clock(now)?, now.date(), 30, false)
        .await?
        .ok_or_else(|| io::Error::other("Expired synthetic FIT job was not recovered"))?;
    assert_eq!(recovered.id(), expiring.id());
    assert!(matches!(
        db.authorize_legacy_fit_job(&request(at(6)?), &expiring)
            .await,
        Err(FitIngestError::Lease(JobStoreError::LeaseLost))
    ));
    assert_eq!(
        db.ingest_leased_legacy_fit(
            request(at(7)?),
            &recovered,
            Cursor::new(FIRST),
            archive.clone(),
            put_limits,
            limits,
        )
        .await?,
        FitIngestOutcome::Normalized { rows: 1 }
    );
    assert_eq!(visible(inspect).await?.2, [72]);
    assert_eq!(
        job_status(inspect, recovered.id()).await?,
        ("done".to_owned(), 2, None)
    );
    let good = freshness(inspect)
        .await?
        .ok_or("Successful fetch did not advance freshness")?;
    assert_eq!(good["status"], "normalized");
    assert_eq!(good["normalized_at"], at(7)?.to_string());

    let rejected = claim(db, inspect, fingerprint, 30).await?;
    let mut corrupted = NEXT.to_vec();
    let last = corrupted.last_mut().ok_or("Missing synthetic checksum")?;
    *last ^= 1;
    assert!(matches!(
        db.ingest_leased_legacy_fit(
            request(at(8)?),
            &rejected,
            Cursor::new(corrupted),
            archive.clone(),
            put_limits,
            limits,
        )
        .await?,
        FitIngestOutcome::Error { .. }
    ));
    assert_eq!(
        job_status(inspect, rejected.id()).await?,
        ("pending".to_owned(), 1, Some("ValueError".to_owned()))
    );
    let parsed = freshness(inspect)
        .await?
        .ok_or("Parse failure was not recorded")?;
    assert_eq!(parsed["status"], "error");
    assert_eq!(parsed["success_at"], at(8)?.to_string());
    assert_eq!(parsed["normalized_at"], good["normalized_at"]);
    assert_eq!(visible(inspect).await?.2, [72]);

    let throttled = claim(db, inspect, fingerprint, 30).await?;
    let retry = OffsetDateTime::now_utc()
        .checked_add(time::Duration::hours(1))
        .ok_or("Invalid retry")?;
    db.finish_legacy_fit_fetch_error(
        request(at(9)?),
        &throttled,
        FailureReason::ProviderCooldown,
        Some(clock(retry)?),
    )
    .await?;
    assert_eq!(
        job_status(inspect, throttled.id()).await?,
        ("pending".to_owned(), 0, Some("ProviderCooldown".to_owned()))
    );
    let fetch = freshness(inspect)
        .await?
        .ok_or("Fetch failure was not recorded")?;
    assert_eq!(fetch["status"], "fetch_error");
    assert_eq!(fetch["success_at"], parsed["success_at"]);
    assert_eq!(fetch["normalized_at"], good["normalized_at"]);
    assert!(fetch["source_ref"].is_null());

    let obsolete = claim(db, inspect, fingerprint, 30).await?;
    assert_eq!(
        db.ingest_leased_legacy_fit(
            request(at(6)?),
            &obsolete,
            Cursor::new(NEXT),
            archive,
            put_limits,
            limits,
        )
        .await?,
        FitIngestOutcome::Stale
    );
    assert_eq!(
        job_status(inspect, obsolete.id()).await?,
        ("done".to_owned(), 1, None)
    );
    assert_eq!(freshness(inspect).await?, Some(fetch));
    Ok(())
}
