use std::{env, error::Error, fs, io, str::FromStr, sync::Arc};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::{ApplyHealthPayload, DocumentStatus},
    jobs::{JobStoreError, RecordedJobStatus, ReplayJobLease},
    raw_capture::LegacyCapture,
    replay::{CalendarReplayError, CalendarReplayOutcome, RunHealthReplay},
};
use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

const DAY: &str = "2026-10-29";

async fn clock(db: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(db)
        .await?;
    Ok(now.try_into()?)
}

async fn enqueue(
    db: &PgPool,
    id: Uuid,
    fingerprint: &str,
    target: i32,
    key: &str,
) -> Result<Uuid, Box<dyn Error>> {
    let job = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES ($1,'raw_replay',$2::jsonb,$3,'pending',clock_timestamp()-interval '2 seconds',0)",
    )
    .bind(job)
    .bind(
        json!({
            "raw_ref": id.to_string(), "account": fingerprint, "target_version": target,
            "backfill": true,
        })
        .to_string(),
    )
    .bind(key)
    .execute(db)
    .await?;
    Ok(job)
}

fn request(lease: &ReplayJobLease) -> RunHealthReplay<'_> {
    RunHealthReplay {
        lease,
        configured_timezone: "America/New_York",
        max_json_bytes: 512_000,
    }
}

async fn visible(
    db: &PgPool,
    raw: Uuid,
    job: Uuid,
) -> Result<(i32, Option<f64>, Option<String>, String, i32), Box<dyn Error>> {
    Ok(sqlx::query_as(
        "SELECT raw.parser_version,
                (SELECT sleep_score::double precision FROM health_days WHERE day='2026-10-29'),
                (SELECT value->>'status' FROM app_state WHERE key=concat('replay:',raw.id::text,':9')),
                job.status,job.attempts
         FROM source_payloads AS raw CROSS JOIN jobs AS job WHERE raw.id=$1 AND job.id=$2",
    )
    .bind(raw)
    .bind(job)
    .fetch_one(db)
    .await?)
}

#[tokio::test]
#[ignore = "uses a freshly initialized disposable TimescaleDB and a synthetic archive"]
async fn replay_loses_lease_without_changing_original_data_and_recovers()
-> Result<(), Box<dyn Error>> {
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
    let inspect = PgPool::connect_with(options).await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let fingerprint = "a".repeat(64);
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES
           ('account:garmin',jsonb_build_object('fingerprint',$1::text))",
    )
    .bind(&fingerprint)
    .execute(&inspect)
    .await?;
    let scratch = tempfile::tempdir()?;
    let path = scratch.path().canonicalize()?.join("archive");
    let archive = Arc::new(Archive::open(&path)?);
    let now = clock(&inspect).await?;
    let raw = db
        .capture_legacy_json(
            LegacyCapture {
                authenticated_fingerprint: &fingerprint,
                endpoint: "daily",
                source_key: DAY,
                fetched_at: now,
                body: br#"{"totalSteps":40}"#.to_vec(),
            },
            archive.clone(),
            PutLimits {
                max_bytes: 512_000,
                disk_reserve_bytes: 0,
            },
        )
        .await?;
    assert_eq!(
        db.apply_health_payload(ApplyHealthPayload {
            account_fingerprint: &fingerprint,
            endpoint: "daily",
            calendar_key: DAY,
            source_id: raw.id,
            requested_at: now,
            timezone: "Europe/Bratislava",
        },)
            .await?,
        DocumentStatus::Normalized
    );
    sqlx::query("UPDATE source_payloads SET parser_version=8 WHERE id=$1")
        .bind(raw.id)
        .execute(&inspect)
        .await?;
    sqlx::query(
        "UPDATE health_days SET sleep_score=72, sources=sources ||
             jsonb_build_object('field:sleep_score',$1::uuid::text,'time:sleep_score',$2::text)
         WHERE day='2026-10-29'",
    )
    .bind(raw.id)
    .bind(now.to_string())
    .execute(&inspect)
    .await?;

    let obsolete = enqueue(
        &inspect,
        raw.id,
        &fingerprint,
        8,
        "synthetic-replay-obsolete",
    )
    .await?;
    let job = enqueue(
        &inspect,
        raw.id,
        &fingerprint,
        9,
        "synthetic-replay-current",
    )
    .await?;
    let activity = db
        .capture_legacy_json(
            LegacyCapture {
                authenticated_fingerprint: &fingerprint,
                endpoint: "activity",
                source_key: "1",
                fetched_at: now,
                body: br#"{"activityId":1}"#.to_vec(),
            },
            archive.clone(),
            PutLimits {
                max_bytes: 512_000,
                disk_reserve_bytes: 0,
            },
        )
        .await?;
    let unhandled = enqueue(
        &inspect,
        activity.id,
        &fingerprint,
        9,
        "synthetic-replay-other",
    )
    .await?;
    let exhausted = enqueue(
        &inspect,
        activity.id,
        &fingerprint,
        9,
        "synthetic-replay-other-exhausted",
    )
    .await?;
    sqlx::query(
        "UPDATE jobs SET run_at=clock_timestamp()-interval '5 minutes',
                    attempts=CASE WHEN id=$2 THEN 8 ELSE 0 END WHERE id IN ($1,$2)",
    )
    .bind(unhandled)
    .bind(exhausted)
    .execute(&inspect)
    .await?;
    let health_exhausted = enqueue(
        &inspect,
        raw.id,
        &fingerprint,
        9,
        "synthetic-health-exhausted",
    )
    .await?;
    sqlx::query("UPDATE jobs SET attempts=8 WHERE id=$1")
        .bind(health_exhausted)
        .execute(&inspect)
        .await?;
    let first = db
        .claim_health_replay_job(clock(&inspect).await?, 30)
        .await?
        .ok_or("Missing replay lease")?;
    assert_eq!((first.id(), first.attempt()), (job, 1));
    assert!(db.renew_replay_job(&first, clock(&inspect).await?).await?);

    // Force the lease to expire inside the final projection transaction. The
    // sequence records that the write was attempted even if it rolls back.
    sqlx::raw_sql(
        "CREATE SEQUENCE synthetic_replay_started;
         CREATE FUNCTION expire_replay_during_publication() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN
             PERFORM nextval('synthetic_replay_started');
             UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'
             WHERE kind='raw_replay' AND status='running';
             RETURN NEW;
         END $$;
         CREATE TRIGGER synthetic_replay_expires BEFORE UPDATE ON health_days
         FOR EACH ROW EXECUTE FUNCTION expire_replay_during_publication();",
    )
    .execute(&inspect)
    .await?;
    assert!(matches!(
        db.run_health_replay_job(request(&first), archive.clone(), || false)
            .await,
        Err(CalendarReplayError::Job(JobStoreError::LeaseLost))
    ));
    assert_eq!(
        visible(&inspect, raw.id, job).await?,
        (8, Some(72.0), None, "running".to_owned(), 1),
    );
    assert!(
        sqlx::query_scalar::<_, bool>("SELECT is_called FROM synthetic_replay_started")
            .fetch_one(&inspect)
            .await?
    );
    sqlx::raw_sql(
        "DROP TRIGGER synthetic_replay_expires ON health_days;
         DROP FUNCTION expire_replay_during_publication();
         DROP SEQUENCE synthetic_replay_started;",
    )
    .execute(&inspect)
    .await?;
    sqlx::query("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1")
        .bind(job)
        .execute(&inspect)
        .await?;

    let retry = db
        .claim_health_replay_job(clock(&inspect).await?, 30)
        .await?
        .ok_or("Missing retry")?;
    assert_eq!((retry.id(), retry.attempt()), (job, 2));
    assert!(!db.renew_replay_job(&first, clock(&inspect).await?).await?);
    assert!(matches!(
        db.run_health_replay_job(request(&first), archive.clone(), || false)
            .await,
        Err(CalendarReplayError::Job(JobStoreError::LeaseLost))
    ));
    let success = db
        .run_health_replay_job(request(&retry), archive.clone(), || false)
        .await?;
    assert_eq!(
        success.replay,
        CalendarReplayOutcome::Applied(DocumentStatus::Normalized)
    );
    assert_eq!(success.job, RecordedJobStatus::Completed);
    assert_eq!(
        visible(&inspect, raw.id, job).await?,
        (9, None, Some("normalized".to_owned()), "done".to_owned(), 2),
    );
    assert!(
        db.claim_health_replay_job(clock(&inspect).await?, 30)
            .await?
            .is_none()
    );
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT status FROM jobs WHERE id=$1")
            .bind(obsolete)
            .fetch_one(&inspect)
            .await?,
        "pending",
    );

    let unaffected: Vec<(String, i32)> =
        sqlx::query_as("SELECT status, attempts FROM jobs WHERE id IN ($1,$2) ORDER BY attempts")
            .bind(unhandled)
            .bind(exhausted)
            .fetch_all(&inspect)
            .await?;
    assert_eq!(
        unaffected,
        vec![("pending".to_owned(), 0), ("pending".to_owned(), 8)]
    );

    let final_state: (String, String) =
        sqlx::query_as("SELECT status,last_error FROM jobs WHERE id=$1")
            .bind(health_exhausted)
            .fetch_one(&inspect)
            .await?;
    assert_eq!(
        final_state,
        ("failed".to_owned(), "RetryLimitExceeded".to_owned())
    );

    let object = path.join(raw.archive_key.to_string());
    let mut data = fs::read(&object)?;
    data[0] = b'[';
    fs::write(&object, data)?;
    let corrupt = enqueue(
        &inspect,
        raw.id,
        &fingerprint,
        9,
        "synthetic-replay-corrupt",
    )
    .await?;
    let lease = db
        .claim_health_replay_job(clock(&inspect).await?, 30)
        .await?
        .ok_or("Missing corrupt job")?;
    let rejected = db
        .run_health_replay_job(request(&lease), archive.clone(), || false)
        .await?;
    assert_eq!(rejected.replay, CalendarReplayOutcome::Failed);
    assert_eq!(rejected.job, RecordedJobStatus::Deferred);
    assert_eq!(
        visible(&inspect, raw.id, corrupt).await?,
        (9, None, Some("error".to_owned()), "pending".to_owned(), 1),
    );

    let observed: StoredInstant = "2026-10-29T10:30:00+00:00".parse()?;
    let raw = db
        .capture_legacy_json(
            LegacyCapture {
                authenticated_fingerprint: &fingerprint,
                endpoint: "heart_rate",
                source_key: DAY,
                fetched_at: now,
                body: serde_json::to_vec(
                    &json!({"heartRateValues":[[observed.unix_microseconds()/1000,77]]}),
                )?,
            },
            archive.clone(),
            PutLimits {
                max_bytes: 512_000,
                disk_reserve_bytes: 0,
            },
        )
        .await?;
    assert_eq!(
        db.apply_health_payload(ApplyHealthPayload {
            account_fingerprint: &fingerprint,
            endpoint: "heart_rate",
            calendar_key: DAY,
            source_id: raw.id,
            requested_at: now,
            timezone: "Europe/Bratislava",
        })
        .await?,
        DocumentStatus::Normalized
    );
    sqlx::query("UPDATE source_payloads SET parser_version=8 WHERE id=$1")
        .bind(raw.id)
        .execute(&inspect)
        .await?;
    sqlx::query("UPDATE measurements SET value=33 WHERE source_ref=$1 AND metric='heart_rate_bpm'")
        .bind(raw.id)
        .execute(&inspect)
        .await?;
    let intraday = enqueue(
        &inspect,
        raw.id,
        &fingerprint,
        9,
        "synthetic-replay-intraday",
    )
    .await?;
    let lease = db
        .claim_health_replay_job(clock(&inspect).await?, 30)
        .await?
        .ok_or("Missing intraday job")?;
    assert_eq!(lease.id(), intraday);
    assert!(matches!(
        db.run_health_replay_job(request(&lease), archive.clone(), || true)
            .await,
        Err(CalendarReplayError::Cancelled)
    ));
    let sample_row = || async {
        sqlx::query_as::<_, (i32, f64, String)>(
            "SELECT raw.parser_version, m.value::float8, j.status
             FROM source_payloads AS raw
             JOIN measurements AS m ON m.source_ref=raw.id
             JOIN jobs AS j ON j.id=$2
             WHERE raw.id=$1 AND m.metric='heart_rate_bpm'",
        )
        .bind(raw.id)
        .bind(intraday)
        .fetch_one(&inspect)
        .await
    };
    assert_eq!(sample_row().await?, (8, 33.0, "running".to_owned()));
    let success = db
        .run_health_replay_job(request(&lease), archive, || false)
        .await?;
    assert_eq!(
        success.replay,
        CalendarReplayOutcome::Applied(DocumentStatus::Normalized)
    );
    assert_eq!(success.job, RecordedJobStatus::Completed);
    assert_eq!(sample_row().await?, (9, 77.0, "done".to_owned()));
    Ok(())
}
