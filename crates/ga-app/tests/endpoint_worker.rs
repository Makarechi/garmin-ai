use std::{
    env,
    error::Error,
    io::{self, Read},
    num::{NonZeroU64, NonZeroUsize},
    str::FromStr,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use ga_app::garmin_worker::{
    ActivityExportSource, ActivityFitWork, ActivityPageWork, EndpointSource, EndpointWork,
    EndpointWorker, EndpointWorkerError, GarminQueueStep, ScheduledGarminWorker,
};
use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    activity_scans::{ActivityPageResult, ActivityScansStarted, StartActivityScans},
    fit_ingest::{FitIngestError, FitIngestOutcome},
    garmin_endpoint::EndpointPublication,
    garmin_integration::GarminTransportOutcome,
    garmin_periodic::GarminSyncSchedule,
    jobs::{GarminJobLease, JobStoreError, RecordedJobStatus},
};
use ga_domain::{event_input::source_calendar_date, timestamp::StoredInstant};
use ga_fit::ExportLimits;
use ga_garmin::{
    GarminError,
    legacy::{DailyEndpoint, LegacyRead},
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{OffsetDateTime, format_description::well_known::Iso8601};
use tokio::{fs::File, io::AsyncWriteExt, sync::Notify};
use uuid::Uuid;

#[path = "endpoint_worker/history_workload.rs"]
mod history_workload;

const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct Source {
    calls: AtomicUsize,
    response: Result<Option<Vec<u8>>, GarminError>,
    delay: Duration,
    activity_offset: Option<u32>,
    started: Option<Arc<Notify>>,
}

impl Source {
    fn new(response: Result<Option<Vec<u8>>, GarminError>, delay: Duration) -> Self {
        Self {
            calls: AtomicUsize::new(0),
            response,
            delay,
            activity_offset: None,
            started: None,
        }
    }
}

impl EndpointSource for Source {
    fn fingerprint(&self) -> &str {
        ACCOUNT
    }

    async fn read(
        &self,
        resource: LegacyRead,
        maximum_bytes: u64,
    ) -> Result<Option<Vec<u8>>, GarminError> {
        if let Some(offset) = self.activity_offset {
            assert!(
                matches!(resource, LegacyRead::Activities {start, limit} if start == offset && limit.get()==100)
            );
        } else {
            assert!(matches!(
                resource,
                LegacyRead::Daily(DailyEndpoint::HeartRate, _)
            ));
        }
        assert_eq!(maximum_bytes, 65_536);
        self.calls.fetch_add(1, Ordering::Relaxed);
        if let Some(started) = &self.started {
            started.notify_one();
        }
        tokio::time::sleep(self.delay).await;
        self.response.clone()
    }
}

struct Fixture {
    database: Database,
    inspect: PgPool,
    archive: Arc<Archive>,
    start: OffsetDateTime,
}

impl Fixture {
    async fn queue(&self, seconds: u32) -> Result<GarminJobLease, Box<dyn Error>> {
        let id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
             VALUES($1,'garmin_endpoint',$2::jsonb,$3,'pending',clock_timestamp(),0)",
        )
        .bind(id)
        .bind(json!({"endpoint":"heart_rate","key":self.start.date().to_string()}).to_string())
        .bind(format!("synthetic:{id}"))
        .execute(&self.inspect)
        .await?;
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&self.inspect)
            .await?;
        let lease = self
            .database
            .claim_garmin_job(now.try_into()?, self.start.date(), seconds, false)
            .await?
            .ok_or("Expected eligible Garmin job")?;
        assert_eq!(lease.id(), id);
        Ok(lease)
    }

    async fn job(&self, lease: &GarminJobLease) -> Result<(String, i32), sqlx::Error> {
        sqlx::query_as("SELECT status,attempts FROM jobs WHERE id=$1")
            .bind(lease.id())
            .fetch_one(&self.inspect)
            .await
    }

    async fn stored(&self, key: &str) -> Result<Value, Box<dyn Error>> {
        let document: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(key)
            .fetch_one(&self.inspect)
            .await?;
        Ok(serde_json::from_str(&document)?)
    }

    fn worker<'a, S: EndpointSource>(&'a self, source: &'a S) -> EndpointWorker<'a, S> {
        EndpointWorker {
            database: &self.database,
            source,
            archive: self.archive.clone(),
            limits: PutLimits {
                max_bytes: 65_536,
                disk_reserve_bytes: 0,
            },
            timezone: "UTC",
        }
    }
}

async fn setup() -> Result<(Fixture, tempfile::TempDir), Box<dyn Error>> {
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
    let start = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&inspect)
        .await?;
    let fixture = Fixture {
        database,
        inspect,
        archive,
        start,
    };
    sqlx::query("INSERT INTO app_state(key,value) VALUES('account:garmin',jsonb_build_object('fingerprint',$1::text))")
        .bind(ACCOUNT).execute(&fixture.inspect).await?;
    Ok((fixture, root))
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and a synthetic Garmin transport"]
async fn defers_busy_endpoint_without_spending_its_attempt() -> Result<(), Box<dyn Error>> {
    let (fixture, _root) = setup().await?;
    let lease = fixture.queue(20).await?;
    let source = Source::new(
        Ok(Some(serde_json::to_vec(&json!({
            "heartRateValues":[[fixture.start.unix_timestamp() * 1_000,77]]
        }))?)),
        Duration::ZERO,
    );
    let worker = fixture.worker(&source);
    let mut blocker = fixture.inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619::bigint)")
        .execute(&mut *blocker)
        .await?;
    assert_eq!(
        Box::pin(worker.run(&lease)).await?,
        EndpointWork::Deferred(RecordedJobStatus::Deferred),
    );
    assert_eq!(fixture.job(&lease).await?, ("pending".into(), 0));
    assert_eq!(source.calls.load(Ordering::Relaxed), 0);
    let state: (bool, bool, bool) = sqlx::query_as(
        "SELECT lease_token IS NULL, lease_until IS NULL, run_at>clock_timestamp()
         FROM jobs WHERE id=$1",
    )
    .bind(lease.id())
    .fetch_one(&fixture.inspect)
    .await?;
    assert_eq!(state, (true, true, true));
    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM source_payloads),(SELECT count(*) FROM measurements)",
    )
    .fetch_one(&fixture.inspect)
    .await?;
    assert_eq!(counts, (0, 0));
    blocker.commit().await?;
    tokio::time::sleep(Duration::from_millis(2100)).await;
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&fixture.inspect)
        .await?;
    let retry = fixture
        .database
        .claim_garmin_job(now.try_into()?, now.date(), 20, false)
        .await?
        .ok_or("Expected due history retry")?;
    assert_eq!(retry.id(), lease.id());
    assert_eq!(
        Box::pin(worker.run(&retry)).await?,
        EndpointWork::Publication(EndpointPublication::Published(DocumentStatus::Normalized)),
    );
    assert_eq!(fixture.job(&retry).await?, ("done".into(), 1));
    assert_eq!(source.calls.load(Ordering::Relaxed), 1);
    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM source_payloads),(SELECT count(*) FROM measurements)",
    )
    .fetch_one(&fixture.inspect)
    .await?;
    assert_eq!(counts, (1, 1));
    Ok(())
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and a synthetic Garmin transport"]
async fn retains_download_during_short_capture_contention() -> Result<(), Box<dyn Error>> {
    let (fixture, _root) = setup().await?;
    let lease = fixture.queue(20).await?;
    let signal = Arc::new(Notify::new());
    let mut source = Source::new(
        Ok(Some(serde_json::to_vec(&json!({
            "heartRateValues":[[fixture.start.unix_timestamp() * 1_000,77]]
        }))?)),
        Duration::from_millis(130),
    );
    source.started = Some(signal.clone());
    let hold_capture = async {
        signal.notified().await;
        let mut blocker = fixture.inspect.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock(72104619::bigint)")
            .execute(&mut *blocker)
            .await?;
        tokio::time::sleep(Duration::from_millis(180)).await;
        blocker.commit().await
    };
    let worker = fixture.worker(&source);
    let (result, released) = tokio::join!(worker.run(&lease), hold_capture);
    released?;
    assert_eq!(
        result?,
        EndpointWork::Publication(EndpointPublication::Published(DocumentStatus::Normalized)),
    );
    assert_eq!(fixture.job(&lease).await?, ("done".into(), 1));
    assert_eq!(source.calls.load(Ordering::Relaxed), 1);
    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM source_payloads),(SELECT count(*) FROM measurements)",
    )
    .fetch_one(&fixture.inspect)
    .await?;
    assert_eq!(counts, (1, 1));
    Ok(())
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and a synthetic Garmin transport"]
async fn reports_durable_endpoint_publication_when_transport_status_is_busy()
-> Result<(), Box<dyn Error>> {
    let (fixture, _root) = setup().await?;
    let lease = fixture.queue(20).await?;
    let source = Source::new(
        Ok(Some(serde_json::to_vec(&json!({
            "heartRateValues":[[fixture.start.unix_timestamp() * 1_000,77]]
        }))?)),
        Duration::ZERO,
    );
    let mut blocker = fixture.inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104624::bigint)")
        .execute(&mut *blocker)
        .await?;
    let result = Box::pin(fixture.worker(&source).run(&lease)).await;
    assert_eq!(fixture.job(&lease).await?, ("done".into(), 1));
    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM source_payloads),(SELECT count(*) FROM measurements)",
    )
    .fetch_one(&fixture.inspect)
    .await?;
    assert_eq!(counts, (1, 1));
    assert_eq!(source.calls.load(Ordering::Relaxed), 1);
    assert_eq!(
        result?,
        EndpointWork::Publication(EndpointPublication::Published(DocumentStatus::Normalized)),
    );
    blocker.commit().await?;
    Ok(())
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and a synthetic Garmin transport"]
async fn publishes_late_response_and_pauses_already_leased_work() -> Result<(), Box<dyn Error>> {
    let (fixture, _root) = setup().await?;
    // Each statement fits the database's 750ms limit, but together they need more than the lease.
    sqlx::raw_sql(
        "CREATE FUNCTION slow_endpoint_publication() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN
             PERFORM pg_sleep(0.55);
             RETURN NEW;
         END
         $$;
         CREATE TRIGGER slow_endpoint_measurement BEFORE INSERT ON measurements
             FOR EACH ROW EXECUTE FUNCTION slow_endpoint_publication();
         CREATE TRIGGER slow_endpoint_freshness BEFORE INSERT ON app_state
             FOR EACH ROW WHEN (NEW.key LIKE 'freshness:heart_rate:%')
             EXECUTE FUNCTION slow_endpoint_publication();",
    )
    .execute(&fixture.inspect)
    .await?;
    let first = fixture.queue(1).await?;
    let recent = fixture.start.unix_timestamp() * 1_000 - 60_000;
    let body = |value| {
        serde_json::to_vec(&json!({
            "heartRateValues":[[recent,value]], "extra":{"retained": true}
        }))
    };
    let success = Source::new(Ok(Some(body(77)?)), Duration::from_millis(1300));
    assert_eq!(
        Box::pin(fixture.worker(&success).run(&first)).await?,
        EndpointWork::Publication(EndpointPublication::Published(DocumentStatus::Normalized)),
    );
    assert_eq!(fixture.job(&first).await?, ("done".into(), 1));
    assert_eq!(success.calls.load(Ordering::Relaxed), 1);
    sqlx::raw_sql(
        "DROP TRIGGER slow_endpoint_measurement ON measurements;
         DROP TRIGGER slow_endpoint_freshness ON app_state;
         DROP FUNCTION slow_endpoint_publication();",
    )
    .execute(&fixture.inspect)
    .await?;
    let rate = fixture.queue(10).await?;
    let preleased = fixture.queue(10).await?;
    let limited = Source::new(
        Err(GarminError::RateLimited {
            retry_after: Duration::from_secs(60),
        }),
        Duration::ZERO,
    );
    assert!(matches!(
        Box::pin(fixture.worker(&limited).run(&rate)).await,
        Err(EndpointWorkerError::Fetch(GarminError::RateLimited { .. }))
    ));
    let state = fixture.stored("integration:garmin").await?;
    assert_eq!(state["status"], "rate_limited");
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&fixture.inspect)
        .await?;
    let blocked: StoredInstant = state["blocked_until"]
        .as_str()
        .ok_or("Expected deadline")?
        .parse()?;
    assert!(OffsetDateTime::from(blocked) - now > time::Duration::seconds(898));
    assert_eq!(fixture.job(&rate).await?, ("pending".into(), 0));
    assert_eq!(
        Box::pin(fixture.worker(&success).run(&preleased)).await?,
        EndpointWork::Deferred(RecordedJobStatus::Deferred),
    );
    assert_eq!(fixture.job(&preleased).await?, ("pending".into(), 0));
    assert_eq!(success.calls.load(Ordering::Relaxed), 1);
    let freshness = fixture
        .stored(&format!("freshness:heart_rate:{}", fixture.start.date()))
        .await?;
    assert_eq!(freshness["status"], "fetch_error");
    assert!(freshness["success_at"].is_string());

    fixture
        .database
        .record_garmin_transport(
            ACCOUNT,
            now.try_into()?,
            GarminTransportOutcome::AuthorizedLogin,
        )
        .await?;
    let expired = fixture.queue(1).await?;
    let blocked_source = Source::new(Ok(Some(body(79)?)), Duration::from_millis(1600));
    let worker = fixture.worker(&blocked_source);
    let revoke = async {
        tokio::time::sleep(Duration::from_millis(100)).await;
        sqlx::query("UPDATE jobs SET lease_token=gen_random_uuid() WHERE id=$1")
            .bind(expired.id())
            .execute(&fixture.inspect)
            .await
    };
    let (outcome, action) = tokio::join!(worker.run(&expired), revoke);
    assert_eq!(action?.rows_affected(), 1);
    assert!(matches!(
        outcome,
        Err(EndpointWorkerError::Job(JobStoreError::LeaseLost))
    ));
    assert_eq!(blocked_source.calls.load(Ordering::Relaxed), 1);
    assert_eq!(fixture.job(&expired).await?, ("running".into(), 1));

    let next = fixture.queue(10).await?;
    let late_response = Source::new(Ok(Some(body(81)?)), Duration::from_millis(300));
    let worker = fixture.worker(&late_response);
    let revoke = async {
        tokio::time::sleep(Duration::from_millis(80)).await;
        sqlx::query(
            "INSERT INTO module_configs(id,owner_id,pack_key,tracking_enabled,collection_enabled,
             reminders_enabled,visible,llm_enabled,settings,revision)
             SELECT gen_random_uuid(),id,'wellbeing',false,false,false,false,false,'{}',1 FROM people",
        ).execute(&fixture.inspect).await
    };
    let (outcome, action) = tokio::join!(worker.run(&next), revoke);
    assert_eq!(action?.rows_affected(), 1);
    assert_eq!(
        outcome?,
        EndpointWork::Publication(EndpointPublication::CollectionDisabled)
    );
    assert_eq!(late_response.calls.load(Ordering::Relaxed), 1);
    assert_eq!(fixture.job(&next).await?, ("done".into(), 1));
    let facts: (i64, f64, bool) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM source_payloads),
            (SELECT value FROM measurements WHERE metric='heart_rate_bpm'),
            (SELECT payload->'extra'->>'retained'='true' FROM source_payloads)",
    )
    .fetch_one(&fixture.inspect)
    .await?;
    assert_eq!(facts, (1, 77.0, true));
    Ok(())
}

#[tokio::test]
#[ignore = "uses a disposable database and synthetic Garmin activity replies"]
async fn restores_the_pending_page_and_refuses_a_superseded_response() -> Result<(), Box<dyn Error>>
{
    let (fixture, _root) = setup().await?;
    let now = fixture.start;
    let account = ACCOUNT;
    assert_eq!(
        fixture
            .database
            .start_activity_scans(StartActivityScans {
                authenticated_fingerprint: account,
                timezone: "UTC",
                history_days: 0,
                now: now.try_into()?,
            })
            .await?,
        ActivityScansStarted::Scheduled { pages: 1 },
    );
    let claim = |seconds| {
        let fixture = &fixture;
        async move {
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&fixture.inspect)
                .await?;
            fixture
                .database
                .claim_garmin_job(at.try_into()?, now.date(), seconds, false)
                .await?
                .ok_or_else(|| -> Box<dyn Error> {
                    io::Error::other("Missing activity claim").into()
                })
        }
    };
    let page: GarminJobLease = claim(1).await?;
    let source_value = json!([{
        "activityId":761,
        "startTimeGMT":(now-time::Duration::seconds(3)).format(&Iso8601::DEFAULT)?,
        "duration":30,
        "unknownSyntheticMetadata":{"kept":true},
    }]);
    let mut source = Source::new(
        Ok(Some(serde_json::to_vec(&source_value)?)),
        Duration::from_millis(1300),
    );
    source.activity_offset = Some(0);
    sqlx::raw_sql(
        "CREATE SEQUENCE synthetic_archiving_entered;
         CREATE FUNCTION slow_activity_capture() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN
            PERFORM nextval('synthetic_archiving_entered');
            PERFORM pg_sleep(0.45);
            RETURN NEW;
         END $$;
         CREATE TRIGGER slow_activity_capture BEFORE INSERT ON source_payloads
            FOR EACH ROW WHEN (NEW.endpoint='activities') EXECUTE FUNCTION slow_activity_capture();
         CREATE FUNCTION slow_activity_publication() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN PERFORM pg_sleep(0.57); RETURN NEW; END $$;
         CREATE TRIGGER slow_activity_row BEFORE INSERT ON activities FOR EACH ROW
            EXECUTE FUNCTION slow_activity_publication();
         CREATE TRIGGER slow_activity_freshness BEFORE INSERT ON app_state
            FOR EACH ROW WHEN (starts_with(NEW.key,'freshness:activities:'))
            EXECUTE FUNCTION slow_activity_publication();",
    )
    .execute(&fixture.inspect)
    .await?;
    let verify_renewal = async {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let entered: bool =
                    sqlx::query_scalar("SELECT is_called FROM synthetic_archiving_entered")
                        .fetch_one(&fixture.inspect)
                        .await?;
                if entered {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            // Polling observes the uncommitted capture. A separate connection
            // must still be able to lock the job row, as lease renewal does.
            let mut renew = fixture.inspect.begin().await?;
            let alive: Uuid =
                sqlx::query_scalar("SELECT id FROM jobs WHERE id=$1 FOR UPDATE NOWAIT")
                    .bind(page.id())
                    .fetch_one(&mut *renew)
                    .await?;
            assert_eq!(alive, page.id());
            renew.rollback().await
        })
        .await??;
        Ok::<_, Box<dyn Error>>(())
    };
    let worker = fixture.worker(&source);
    let (result, renewal) = tokio::join!(worker.run_activity_page(&page), verify_renewal);
    renewal?;
    sqlx::raw_sql(
        "DROP TRIGGER slow_activity_capture ON source_payloads;
         DROP FUNCTION slow_activity_capture();
         DROP SEQUENCE synthetic_archiving_entered;
         DROP TRIGGER slow_activity_row ON activities;
         DROP TRIGGER slow_activity_freshness ON app_state;
         DROP FUNCTION slow_activity_publication();",
    )
    .execute(&fixture.inspect)
    .await?;
    assert_eq!(
        result?,
        ActivityPageWork::Publication(ActivityPageResult::Published(DocumentStatus::Normalized)),
    );
    assert_eq!(fixture.job(&page).await?, ("done".into(), 1));
    assert_eq!(source.calls.load(Ordering::Relaxed), 1);
    let key = format!("activity_scan:{ACCOUNT}:recent");
    let state = fixture.stored(&key).await?;
    assert_eq!(state["status"], "complete");
    assert_eq!(state["pages"], 1);
    let (recorded, summary, fit): (String,i64,i64) = sqlx::query_as(
        "SELECT (SELECT payload::text FROM source_payloads WHERE source='garmin_connect'
                 AND endpoint='activities' AND source_key='page:0'),
                (SELECT count(*) FROM activities WHERE id='761'),
                (SELECT count(*) FROM jobs WHERE kind='garmin_fit' AND payload->>'activity_id'='761')",
    ).fetch_one(&fixture.inspect).await?;
    assert_eq!(serde_json::from_str::<Value>(&recorded)?, source_value);
    assert_eq!((summary, fit), (1, 1));

    sqlx::raw_sql(
        "UPDATE jobs SET run_at=clock_timestamp()+interval '1 day' WHERE status='pending';
         UPDATE app_state SET value=jsonb_set(value,'{next_scan_at}','null') WHERE starts_with(key,'activity_scan:');",
    ).execute(&fixture.inspect).await?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&fixture.inspect)
        .await?;
    assert_eq!(
        fixture
            .database
            .start_activity_scans(StartActivityScans {
                authenticated_fingerprint: account,
                timezone: "UTC",
                history_days: 0,
                now: at.try_into()?,
            })
            .await?,
        ActivityScansStarted::Scheduled { pages: 1 },
    );
    let page: GarminJobLease = claim(20).await?;
    let mut late = source_value;
    late[0]["duration"] = json!(91);
    let mut source = Source::new(
        Ok(Some(serde_json::to_vec(&late)?)),
        Duration::from_millis(250),
    );
    source.activity_offset = Some(0);
    let started = Arc::new(Notify::new());
    source.started = Some(started.clone());
    let supersede = async {
        tokio::time::timeout(Duration::from_secs(2), started.notified()).await?;
        sqlx::query(
            "UPDATE app_state SET value=jsonb_set(value,'{generation}',to_jsonb(gen_random_uuid()))
                        WHERE key=$1",
        )
        .bind(&key)
        .execute(&fixture.inspect)
        .await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let worker = fixture.worker(&source);
    let (result, changed) = tokio::join!(worker.run_activity_page(&page), supersede);
    changed?;
    assert_eq!(
        result?,
        ActivityPageWork::Publication(ActivityPageResult::Superseded)
    );
    assert_eq!(fixture.job(&page).await?, ("done".into(), 1));
    assert_eq!(source.calls.load(Ordering::Relaxed), 1);
    let raw_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM source_payloads WHERE endpoint='activities'")
            .fetch_one(&fixture.inspect)
            .await?;
    assert_eq!(raw_count, 1);
    let freshness = fixture.stored("freshness:activities:page:0").await?;
    assert_eq!(freshness["status"], "normalized");
    Ok(())
}

const FIT_RECORDING: &[u8] =
    b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\xb0\xe5";

struct FitSource {
    calls: AtomicUsize,
    interrupted: bool,
    started: Option<Arc<Notify>>,
}

impl EndpointSource for FitSource {
    fn fingerprint(&self) -> &str {
        ACCOUNT
    }

    async fn read(&self, _: LegacyRead, _: u64) -> Result<Option<Vec<u8>>, GarminError> {
        panic!("The FIT job should use the export resource");
    }
}

impl ActivityExportSource for FitSource {
    async fn download(
        &self,
        activity: NonZeroU64,
        maximum_bytes: u64,
        destination: &mut File,
    ) -> Result<u64, GarminError> {
        assert_eq!(activity.get(), 761);
        assert_eq!(maximum_bytes, 65_536);
        self.calls.fetch_add(1, Ordering::Relaxed);
        destination
            .write_all(&FIT_RECORDING[..16])
            .await
            .map_err(|_| GarminError::DestinationWrite)?;
        if let Some(started) = &self.started {
            started.notify_one();
        }
        if self.interrupted {
            return Err(GarminError::InterruptedTransfer);
        }
        tokio::time::sleep(Duration::from_millis(1300)).await;
        destination
            .write_all(&FIT_RECORDING[16..])
            .await
            .map_err(|_| GarminError::DestinationWrite)?;
        Ok(FIT_RECORDING.len() as u64)
    }
}

#[tokio::test]
#[ignore = "uses a disposable database and synthetic streaming Garmin FIT"]
async fn downloads_fit_in_private_staging_and_preserves_the_last_complete_export()
-> Result<(), Box<dyn Error>> {
    let (fixture, root) = setup().await?;
    sqlx::query(
        "INSERT INTO activities(id,kind,start,\"end\",timezone,details)
         VALUES('761','run',$1,$1,'UTC','{}')",
    )
    .bind(fixture.start)
    .execute(&fixture.inspect)
    .await?;
    let claim = |seconds| {
        let fixture = &fixture;
        async move {
            let id = Uuid::new_v4();
            sqlx::query(
                "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
                 VALUES($1,'garmin_fit',$2::jsonb,$3,'pending',clock_timestamp(),0)",
            )
            .bind(id)
            .bind(json!({"activity_id":"761","account":ACCOUNT}).to_string())
            .bind(format!("synthetic:{id}"))
            .execute(&fixture.inspect)
            .await?;
            let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&fixture.inspect)
                .await?;
            let lease = fixture
                .database
                .claim_garmin_job(now.try_into()?, now.date(), seconds, false)
                .await?
                .ok_or_else(|| -> Box<dyn Error> {
                    io::Error::other("Missing FIT claim").into()
                })?;
            assert_eq!(lease.id(), id);
            Ok::<_, Box<dyn Error>>(lease)
        }
    };
    let limits = ExportLimits {
        max_bytes: 65_536,
        max_zip_entries: NonZeroUsize::MIN,
        max_duration: Duration::from_secs(2),
    };
    let good = FitSource {
        calls: AtomicUsize::new(0),
        interrupted: false,
        started: None,
    };
    let first = claim(1).await?;
    sqlx::raw_sql(
        "CREATE SEQUENCE synthetic_fit_capture_entered;
         CREATE FUNCTION slow_fit_capture() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN
            PERFORM nextval('synthetic_fit_capture_entered');
            PERFORM pg_sleep(0.45);
            RETURN NEW;
         END $$;
         CREATE TRIGGER slow_fit_capture BEFORE INSERT ON source_payloads
            FOR EACH ROW WHEN (NEW.endpoint='activity_fit') EXECUTE FUNCTION slow_fit_capture();
         CREATE FUNCTION slow_fit_publication() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN PERFORM pg_sleep(1.18); RETURN NEW; END $$;
         CREATE TRIGGER slow_fit_publication BEFORE INSERT ON activity_parts FOR EACH ROW
            EXECUTE FUNCTION slow_fit_publication();",
    )
    .execute(&fixture.inspect)
    .await?;
    let verify_renewal = async {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let entered: bool =
                    sqlx::query_scalar("SELECT is_called FROM synthetic_fit_capture_entered")
                        .fetch_one(&fixture.inspect)
                        .await?;
                if entered {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            let mut renew = fixture.inspect.begin().await?;
            let alive: Uuid =
                sqlx::query_scalar("SELECT id FROM jobs WHERE id=$1 FOR UPDATE NOWAIT")
                    .bind(first.id())
                    .fetch_one(&mut *renew)
                    .await?;
            assert_eq!(alive, first.id());
            renew.rollback().await
        })
        .await??;
        Ok::<_, Box<dyn Error>>(())
    };
    let worker = fixture.worker(&good);
    let (result, renewal) =
        tokio::join!(worker.run_activity_export(&first, limits), verify_renewal);
    renewal?;
    sqlx::raw_sql(
        "DROP TRIGGER slow_fit_capture ON source_payloads;
         DROP FUNCTION slow_fit_capture();
         DROP SEQUENCE synthetic_fit_capture_entered;
         DROP TRIGGER slow_fit_publication ON activity_parts;
         DROP FUNCTION slow_fit_publication();",
    )
    .execute(&fixture.inspect)
    .await?;
    assert_eq!(
        result?,
        ActivityFitWork::Publication(FitIngestOutcome::Normalized { rows: 1 }),
    );
    assert_eq!(good.calls.load(Ordering::Relaxed), 1);
    assert_eq!(fixture.job(&first).await?, ("done".into(), 1));
    let key: String = sqlx::query_scalar("SELECT fit_key FROM activities WHERE id='761'")
        .fetch_one(&fixture.inspect)
        .await?;
    let mut archived = Vec::new();
    fixture
        .archive
        .open_reader(&key.parse()?)?
        .read_to_end(&mut archived)?;
    assert_eq!(archived, FIT_RECORDING);

    let interrupted = FitSource {
        calls: AtomicUsize::new(0),
        interrupted: true,
        started: None,
    };
    let second = claim(20).await?;
    assert!(matches!(
        fixture
            .worker(&interrupted)
            .run_activity_export(&second, limits)
            .await,
        Err(EndpointWorkerError::Fetch(GarminError::InterruptedTransfer)),
    ));
    assert_eq!(interrupted.calls.load(Ordering::Relaxed), 1);
    assert_eq!(fixture.job(&second).await?, ("pending".into(), 1));
    let actual: (String,i32,i64) = sqlx::query_as(
        "SELECT (SELECT fit_key FROM activities WHERE id='761'),
            (SELECT (payload->>'heart_rate')::int FROM activity_parts WHERE activity_id='761' AND kind='fit_record'),
            (SELECT count(*) FROM source_payloads WHERE source_key='761')"
    ).fetch_one(&fixture.inspect).await?;
    assert_eq!(actual, (key, 72, 1));
    let freshness = fixture.stored("freshness:activity_fit:761").await?;
    assert_eq!(freshness["status"], "fetch_error");
    assert!(freshness["success_at"].is_string());

    for path in std::fs::read_dir(root.path().join("raw"))? {
        assert!(
            !path?
                .file_name()
                .to_string_lossy()
                .starts_with(".rust-stage-")
        );
    }
    Ok(())
}

struct ScheduledSource {
    at: OffsetDateTime,
    calls: AtomicUsize,
    fit: FitSource,
}

impl EndpointSource for ScheduledSource {
    fn fingerprint(&self) -> &str {
        ACCOUNT
    }

    #[expect(
        clippy::unused_async_trait_impl,
        reason = "Keep the async trait method deferred until the returned future is polled"
    )]
    async fn read(
        &self,
        resource: LegacyRead,
        maximum_bytes: u64,
    ) -> Result<Option<Vec<u8>>, GarminError> {
        assert_eq!(maximum_bytes, 65_536);
        self.calls.fetch_add(1, Ordering::Relaxed);
        let value = match resource {
            LegacyRead::Activities { start: 0, limit } if limit.get() == 100 => json!([{
                "activityId":761,
                "startTimeGMT":self.at.unix_timestamp()-5,
                "duration":30
            }]),
            LegacyRead::Daily(DailyEndpoint::HeartRate, _) => json!({
                "heartRateValues":[[(self.at.unix_timestamp() - 60) * 1000,77]]
            }),
            _ => return Err(GarminError::InvalidRequest),
        };
        serde_json::to_vec(&value)
            .map(Some)
            .map_err(|_| GarminError::InvalidRequest)
    }
}

impl ActivityExportSource for ScheduledSource {
    async fn download(
        &self,
        activity: NonZeroU64,
        maximum_bytes: u64,
        destination: &mut File,
    ) -> Result<u64, GarminError> {
        self.fit
            .download(activity, maximum_bytes, destination)
            .await
    }
}

#[tokio::test]
#[ignore = "runs the claim and dispatch stages with disposable TimescaleDB and synthetic Garmin"]
async fn scheduled_worker_dispatches_all_native_garmin_jobs_for_the_current_source()
-> Result<(), Box<dyn Error>> {
    let (fixture, _root) = setup().await?;
    let started = Arc::new(Notify::new());
    let source = ScheduledSource {
        at: fixture.start,
        calls: AtomicUsize::new(0),
        fit: FitSource {
            calls: AtomicUsize::new(0),
            interrupted: false,
            started: Some(started.clone()),
        },
    };
    let mut worker = ScheduledGarminWorker {
        runner: fixture.worker(&source),
        instance: "source:garmin:primary",
        history_days: 0,
        backups_enabled: false,
        fit_limits: ExportLimits {
            max_bytes: 65_536,
            max_zip_entries: NonZeroUsize::MIN,
            max_duration: Duration::from_secs(3),
        },
    };
    let now = || async {
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&fixture.inspect)
            .await?;
        Ok::<_, Box<dyn Error>>(StoredInstant::try_from(now)?)
    };
    let mut onboarding = fixture.inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104621::bigint)")
        .execute(&mut *onboarding)
        .await?;
    assert_eq!(
        Box::pin(worker.process_one(now().await?)).await?,
        GarminQueueStep::Idle
    );
    assert_eq!(source.calls.load(Ordering::Relaxed), 0);
    onboarding.commit().await?;
    assert!(matches!(
        worker.plan(now().await?).await?,
        GarminSyncSchedule::Scheduled{ activity_pages:1, periodic, .. } if periodic > 0,
    ));
    sqlx::query("UPDATE jobs SET run_at=run_at+interval '1 day' WHERE kind='garmin_endpoint'")
        .execute(&fixture.inspect)
        .await?;
    let overdue =
        StoredInstant::try_from(OffsetDateTime::from(now().await?) - time::Duration::hours(1))?;
    assert!(
        fixture
            .database
            .schedule_daily_backup(overdue)
            .await?
            .is_some()
    );
    worker.backups_enabled = true;
    assert_eq!(
        Box::pin(worker.process_one(now().await?)).await?,
        GarminQueueStep::Idle
    );
    assert_eq!(source.calls.load(Ordering::Relaxed), 0);
    worker.backups_enabled = false;
    assert_eq!(
        Box::pin(worker.process_one(now().await?)).await?,
        GarminQueueStep::Activities(ActivityPageWork::Publication(
            ActivityPageResult::Published(DocumentStatus::Normalized)
        )),
    );
    sqlx::raw_sql(
        "UPDATE jobs SET run_at=clock_timestamp()+interval '1 day'
             WHERE kind='garmin_endpoint' AND status='pending';
         INSERT INTO app_state(key,value) VALUES('preferences:onboarding','{\"source_instance_ids\":[]}');"
    ).execute(&fixture.inspect).await?;
    assert_eq!(
        Box::pin(worker.process_one(now().await?)).await?,
        GarminQueueStep::Inactive
    );
    let fit_attempts: i32 = sqlx::query_scalar("SELECT attempts FROM jobs WHERE kind='garmin_fit'")
        .fetch_one(&fixture.inspect)
        .await?;
    assert_eq!(fit_attempts, 0);
    assert_eq!(source.fit.calls.load(Ordering::Relaxed), 0);

    sqlx::query("DELETE FROM app_state WHERE key='preferences:onboarding'")
        .execute(&fixture.inspect)
        .await?;
    let revoke = async {
        tokio::time::timeout(Duration::from_secs(2), started.notified()).await?;
        sqlx::query(
            "INSERT INTO app_state(key,value)
                     VALUES('preferences:onboarding','{\"source_instance_ids\":[]}')",
        )
        .execute(&fixture.inspect)
        .await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let (outcome, revoke) = tokio::join!(worker.process_one(now().await?), revoke);
    revoke?;
    assert!(matches!(
        outcome,
        Err(EndpointWorkerError::Fit(FitIngestError::Lease(
            JobStoreError::LeaseLost
        )))
    ));
    let archived_while_disabled: i64 =
        sqlx::query_scalar("SELECT count(*) FROM source_payloads WHERE endpoint='activity_fit'")
            .fetch_one(&fixture.inspect)
            .await?;
    assert_eq!(archived_while_disabled, 0);
    sqlx::raw_sql(
        "DELETE FROM app_state WHERE key='preferences:onboarding';
         UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'
            WHERE kind='garmin_fit' AND status='running';",
    )
    .execute(&fixture.inspect)
    .await?;
    assert_eq!(
        Box::pin(worker.process_one(now().await?)).await?,
        GarminQueueStep::Fit(ActivityFitWork::Publication(FitIngestOutcome::Normalized {
            rows: 1
        })),
    );
    sqlx::query(
        "UPDATE jobs SET run_at=clock_timestamp()-interval '1 second' WHERE kind='garmin_endpoint'
         AND payload->>'endpoint'='heart_rate' AND payload->>'key'=$1",
    )
    .bind(fixture.start.date().to_string())
    .execute(&fixture.inspect)
    .await?;
    let sample =
        StoredInstant::from_unix_microseconds((source.at.unix_timestamp() - 60) * 1_000_000)?;
    let original = source_calendar_date(sample, worker.runner.timezone);
    let (new_timezone, new_date) = ["Etc/GMT+12", "Pacific/Kiritimati"]
        .into_iter()
        .filter_map(|zone| source_calendar_date(sample, zone).map(|date| (zone, date)))
        .find(|(_, date)| Some(*date) != original)
        .ok_or("Expected a timezone that changes the sample's date")?;
    sqlx::query("UPDATE people SET timezone=$1 WHERE singleton")
        .bind(new_timezone)
        .execute(&fixture.inspect)
        .await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES('preferences:onboarding','{\"source_instance_ids\":[\"source:garmin:primary\"]}')")
        .execute(&fixture.inspect).await?;
    assert_eq!(
        Box::pin(worker.process_one(now().await?)).await?,
        GarminQueueStep::Endpoint(EndpointWork::Publication(EndpointPublication::Published(
            DocumentStatus::Normalized
        ))),
    );
    let observed: String =
        sqlx::query_scalar("SELECT local_date::text FROM measurements WHERE ts=$1")
            .bind(OffsetDateTime::from(sample))
            .fetch_one(&fixture.inspect)
            .await?;
    assert_eq!(observed, new_date.to_string());
    sqlx::query("DELETE FROM app_state WHERE key='preferences:onboarding'")
        .execute(&fixture.inspect)
        .await?;
    assert_eq!(source.calls.load(Ordering::Relaxed), 2);
    assert_eq!(source.fit.calls.load(Ordering::Relaxed), 2);
    assert_eq!(
        Box::pin(worker.process_one(now().await?)).await?,
        GarminQueueStep::Idle
    );
    assert_eq!(
        fixture
            .database
            .retire_deselected_garmin_work(worker.instance, now().await?)
            .await?,
        0
    );
    let next =
        StoredInstant::try_from(OffsetDateTime::from(now().await?) + time::Duration::days(1))?;
    assert!(matches!(
        worker.plan(next).await?,
        GarminSyncSchedule::Scheduled {
            activity_pages: 1,
            ..
        }
    ));
    let scan_key = format!("activity_scan:{ACCOUNT}:recent");
    let scan_state = || {
        sqlx::query_as::<_, (String, String)>(
            "SELECT value->>'status',value->>'generation' FROM app_state WHERE key=$1",
        )
        .bind(&scan_key)
        .fetch_one(&fixture.inspect)
    };
    let pending = scan_state().await?;
    assert_eq!(pending.0, "running");
    sqlx::query("INSERT INTO app_state(key,value) VALUES('preferences:onboarding','{\"source_instance_ids\":[]}')")
        .execute(&fixture.inspect).await?;
    assert!(
        fixture
            .database
            .retire_deselected_garmin_work(worker.instance, next)
            .await?
            > 0
    );
    let disabled = scan_state().await?;
    assert_eq!(disabled.0, "disabled");
    assert_eq!(disabled.1, pending.1);
    sqlx::query("DELETE FROM app_state WHERE key='preferences:onboarding'")
        .execute(&fixture.inspect)
        .await?;
    assert!(matches!(
        worker.plan(next).await?,
        GarminSyncSchedule::Scheduled {
            activity_pages: 1,
            ..
        }
    ));
    let reselected = scan_state().await?;
    assert_eq!(reselected.0, "running");
    assert_ne!(reselected.1, pending.1);
    let old_export_still_visible: bool =
        sqlx::query_scalar("SELECT fit_key IS NOT NULL FROM activities WHERE id='761'")
            .fetch_one(&fixture.inspect)
            .await?;
    assert!(old_export_still_visible);
    Ok(())
}

#[tokio::test]
#[ignore = "requires disposable TimescaleDB and an independently held source-selection lock"]
async fn waits_to_claim_when_source_selection_becomes_busy() -> Result<(), Box<dyn Error>> {
    let (fixture, _root) = setup().await?;
    let now: StoredInstant = fixture.start.try_into()?;
    let instance = "source:garmin:primary";
    let id: Uuid = sqlx::query_scalar(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES (gen_random_uuid(),'garmin_endpoint',
            jsonb_build_object('endpoint','heart_rate','key',$1::text),
            'synthetic-source-selection-race','pending',$2,0) RETURNING id",
    )
    .bind(fixture.start.date().to_string())
    .bind(fixture.start)
    .fetch_one(&fixture.inspect)
    .await?;
    assert!(matches!(
        fixture
            .database
            .garmin_source_admission(instance, now)
            .await?,
        ga_db::garmin_integration::GarminSourceAdmission::Ready { .. }
    ));

    let mut onboarding = fixture.inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104621::bigint)")
        .execute(&mut *onboarding)
        .await?;
    let claim = || {
        fixture
            .database
            .claim_selected_garmin_job(now, fixture.start.date(), 120, false, instance)
    };
    assert!(claim().await?.is_none());
    let untouched: (String, i32, bool) =
        sqlx::query_as("SELECT status,attempts,lease_token IS NULL FROM jobs WHERE id=$1")
            .bind(id)
            .fetch_one(&fixture.inspect)
            .await?;
    assert_eq!(untouched, ("pending".into(), 0, true));
    onboarding.commit().await?;

    let lease = claim().await?.ok_or("Expected the deferred source job")?;
    assert_eq!(lease.id(), id);
    assert_eq!(fixture.job(&lease).await?, ("running".into(), 1));
    Ok(())
}
