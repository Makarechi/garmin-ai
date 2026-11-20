use std::{
    env,
    error::Error,
    io::{self, Cursor},
    num::NonZeroUsize,
    str::FromStr,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    fit_ingest::{FitIngestOutcome, FitReplayOutcome, LegacyFitCapture},
    jobs::{JobStoreError, RecordedJobStatus, ReplayJobLease},
    raw_capture::LegacyCapture,
    replay::{
        CalendarReplayError, CalendarReplayOutcome, ReplayJobOutcome, ReplayWorkerError,
        RunHealthReplay, RunReplayJob,
    },
};
use ga_fit::{ExportLimits, MAX_FIT_BYTES};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const FIT: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\xb0\xe5";

struct Fixture {
    inspect: PgPool,
    database: Database,
    archive: Arc<Archive>,
    at: OffsetDateTime,
}

impl Fixture {
    async fn claim(&self, seconds: u32) -> Result<ReplayJobLease, Box<dyn Error>> {
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&self.inspect)
            .await?;
        self.database
            .claim_replay_job(now.try_into()?, seconds)
            .await?
            .ok_or_else(|| "Expected eligible replay job".into())
    }

    async fn queue(
        &self,
        reference: &str,
        target: i32,
        order: i64,
    ) -> Result<Uuid, Box<dyn Error>> {
        let id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
             VALUES($1,'raw_replay',$2::jsonb,$3,'pending',$4,0)",
        )
        .bind(id)
        .bind(
            json!({"raw_ref":reference,"account":ACCOUNT,"target_version":target,"backfill":true})
                .to_string(),
        )
        .bind(format!("synthetic:{id}"))
        .bind(self.at - time::Duration::seconds(100 - order))
        .execute(&self.inspect)
        .await?;
        Ok(id)
    }

    async fn capture(
        &self,
        endpoint: &str,
        key: &str,
        body: Value,
    ) -> Result<Uuid, Box<dyn Error>> {
        let capture = self
            .database
            .capture_legacy_json(
                LegacyCapture {
                    authenticated_fingerprint: ACCOUNT,
                    endpoint,
                    source_key: key,
                    fetched_at: self.at.try_into()?,
                    body: serde_json::to_vec(&body)?,
                },
                self.archive.clone(),
                PutLimits {
                    max_bytes: 512_000,
                    disk_reserve_bytes: 0,
                },
            )
            .await?;
        Ok(capture.id)
    }

    async fn capture_fit(&self, activity: &str) -> Result<Uuid, Box<dyn Error>> {
        sqlx::query(
            r#"INSERT INTO activities(id,kind,start,"end",timezone,details) VALUES($1,'running',$2,$2,'UTC','{}')"#,
        )
        .bind(activity).bind(self.at).execute(&self.inspect).await?;
        let capture = self
            .database
            .capture_legacy_activity_export(
                LegacyFitCapture {
                    activity_id: activity,
                    authenticated_fingerprint: ACCOUNT,
                    fetched_at: self.at.try_into()?,
                },
                Cursor::new(FIT),
                self.archive.clone(),
                PutLimits {
                    max_bytes: MAX_FIT_BYTES,
                    disk_reserve_bytes: 0,
                },
            )
            .await?;
        assert_eq!(
            self.database
                .apply_legacy_activity_export(&capture, self.archive.clone(), fit_limits())
                .await?,
            FitIngestOutcome::Normalized { rows: 1 }
        );
        sqlx::query("UPDATE source_payloads SET parser_version=8 WHERE id=$1")
            .bind(capture.source_id())
            .execute(&self.inspect)
            .await?;
        sqlx::query(
            "UPDATE activity_parts SET payload=jsonb_set(payload,'{heart_rate}','70')
             WHERE activity_id=$1 AND kind='fit_record'",
        )
        .bind(activity)
        .execute(&self.inspect)
        .await?;
        Ok(capture.source_id())
    }

    fn request<'a>(&self, lease: &'a ReplayJobLease) -> RunReplayJob<'a> {
        RunReplayJob {
            lease,
            configured_timezone: "UTC",
            max_json_bytes: 512_000,
            fit_limits: fit_limits(),
        }
    }

    async fn job(&self, id: Uuid) -> Result<(String, i32), sqlx::Error> {
        sqlx::query_as("SELECT status,attempts FROM jobs WHERE id=$1")
            .bind(id)
            .fetch_one(&self.inspect)
            .await
    }
}

fn fit_limits() -> ExportLimits {
    ExportLimits {
        max_bytes: MAX_FIT_BYTES,
        max_zip_entries: NonZeroUsize::MIN,
        max_duration: Duration::from_secs(10),
    }
}

fn slow_verification() -> impl Fn() -> bool + Clone + Send + 'static {
    let caller = std::thread::current().id();
    let delayed = Arc::new(AtomicBool::new(false));
    move || {
        if caller != std::thread::current().id() && !delayed.swap(true, Ordering::Relaxed) {
            std::thread::sleep(Duration::from_millis(1350));
        }
        false
    }
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB with verified synthetic archives and actual job leases"]
async fn routes_supported_jobs_and_fences_fit_publication() -> Result<(), Box<dyn Error>> {
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
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let root = tempfile::tempdir()?;
    let archive = Arc::new(Archive::open(&root.path().canonicalize()?.join("raw"))?);
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&inspect)
        .await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES('account:garmin',jsonb_build_object('fingerprint',$1::text))")
        .bind(ACCOUNT).execute(&inspect).await?;
    let run = Fixture {
        inspect,
        database,
        archive,
        at,
    };
    let day = at.date().to_string();
    let daily = run
        .capture("daily", &day, json!({"totalSteps": 40}))
        .await?;
    let intraday = run
        .capture(
            "heart_rate",
            &day,
            json!({"heartRateValues": [[at.unix_timestamp() * 1000, 72]]}),
        )
        .await?;
    let summary = run.capture("activity", "10", json!({
        "activityId": 10, "startTimeGMT":"2026-10-29 08:00:00","duration":120,"averageHR":76,
    })).await?;
    let detail = run
        .capture("activity_weather", "10", json!({"wind":4}))
        .await?;
    let mut archived = Vec::new();
    for endpoint in [
        "all_day_events",
        "body_battery_events",
        "body_composition",
        "endurance",
        "hill",
        "intensity",
        "resting_hr",
        "training_status",
        "devices",
    ] {
        let key = if endpoint == "devices" {
            "global"
        } else {
            &day
        };
        archived.push(
            run.capture(endpoint, key, json!({"synthetic": endpoint}))
                .await?,
        );
    }
    let empty = run
        .capture("all_day_events", "2026-10-20", json!([]))
        .await?;
    let fit = run.capture_fit("20").await?;
    let unknown: Uuid = sqlx::query_scalar(
        "INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,fetched_at,parser_version,status,payload)
         SELECT gen_random_uuid(),source,'synthetic_unknown',source_key,payload_hash,archive_key,fetched_at,0,'pending',payload
         FROM source_payloads WHERE id=$1 RETURNING id",
    ).bind(daily).fetch_one(&run.inspect).await?;
    let unsupported = run.queue(&unknown.to_string(), 9, 0).await?;
    let malformed = run.queue("malformed-fixture-uuid", 9, 1).await?;
    let obsolete = run.queue(&fit.to_string(), 8, 2).await?;
    let fit_job = run.queue(&fit.to_string(), 9, 3).await?;
    let fit_lease = run.claim(1).await?;
    assert_eq!(fit_lease.id(), fit_job);
    let result = run
        .database
        .run_replay_job(
            run.request(&fit_lease),
            run.archive.clone(),
            slow_verification(),
        )
        .await?;
    assert_eq!(
        result.replay,
        ReplayJobOutcome::Fit(FitReplayOutcome::Applied(FitIngestOutcome::Normalized {
            rows: 1
        }))
    );
    assert_eq!(result.job, RecordedJobStatus::Completed);
    // The remaining variants use the same dispatch entry point and publish one outcome per lease.
    let documents = [
        (daily, DocumentStatus::Normalized),
        (intraday, DocumentStatus::Normalized),
        (summary, DocumentStatus::Normalized),
        (detail, DocumentStatus::Archived),
        (empty, DocumentStatus::Empty),
    ];
    let documents = documents.into_iter().chain(
        archived
            .into_iter()
            .map(|raw| (raw, DocumentStatus::Archived)),
    );
    for (order, (raw, expected)) in documents.enumerate() {
        let order = i64::try_from(order)? + 4;
        let job = run.queue(&raw.to_string(), 9, order).await?;
        let lease = run.claim(10).await?;
        assert_eq!(lease.id(), job);
        if raw == summary {
            assert!(matches!(
                run.database
                    .run_health_replay_job(
                        RunHealthReplay {
                            lease: &lease,
                            configured_timezone: "UTC",
                            max_json_bytes: 512_000
                        },
                        run.archive.clone(),
                        || false,
                    )
                    .await,
                Err(CalendarReplayError::OtherEndpoint)
            ));
        }
        let result = run
            .database
            .run_replay_job(run.request(&lease), run.archive.clone(), || false)
            .await?;
        assert_eq!(
            result.replay,
            ReplayJobOutcome::Json(CalendarReplayOutcome::Applied(expected))
        );
        assert_eq!(result.job, RecordedJobStatus::Completed);
        assert_eq!(run.job(job).await?, ("done".into(), 1));
    }
    for untouched in [unsupported, malformed, obsolete] {
        assert_eq!(run.job(untouched).await?, ("pending".into(), 0));
    }

    let race = run.capture_fit("21").await?;
    let job = run.queue(&race.to_string(), 9, 8).await?;
    let lease = run.claim(1).await?;
    let invalidation = async {
        tokio::time::sleep(Duration::from_millis(100)).await;
        sqlx::query("UPDATE jobs SET lease_token=gen_random_uuid() WHERE id=$1")
            .bind(job)
            .execute(&run.inspect)
            .await
    };
    let (result, invalidated) = tokio::join!(
        run.database.run_replay_job(
            run.request(&lease),
            run.archive.clone(),
            slow_verification()
        ),
        invalidation
    );
    assert_eq!(invalidated?.rows_affected(), 1);
    assert!(matches!(
        result,
        Err(ReplayWorkerError::Lease(JobStoreError::LeaseLost))
    ));
    let facts: (String, i32, bool) = sqlx::query_as(
        "SELECT (SELECT payload->>'heart_rate' FROM activity_parts WHERE activity_id='21'),
                (SELECT parser_version FROM source_payloads WHERE id=$1),
                EXISTS(SELECT 1 FROM app_state WHERE key=concat('replay:',$1::uuid::text,':9'))",
    )
    .bind(race)
    .fetch_one(&run.inspect)
    .await?;
    assert_eq!(facts, ("70".to_owned(), 8, false));
    sqlx::query("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1")
        .bind(job)
        .execute(&run.inspect)
        .await?;
    let retry = run.claim(10).await?;
    assert_eq!(retry.id(), job);
    let result = run
        .database
        .run_replay_job(run.request(&retry), run.archive.clone(), || false)
        .await?;
    assert_eq!(result.job, RecordedJobStatus::Completed);
    assert_eq!(run.job(job).await?, ("done".into(), 2));
    Ok(())
}
