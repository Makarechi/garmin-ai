use std::{
    env,
    error::Error,
    io,
    str::FromStr,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use ga_app::garmin_worker::{
    ActivityPageWork, EndpointSource, EndpointWork, EndpointWorker, EndpointWorkerError,
};
use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    activity_scans::{ActivityPageResult, ActivityScansStarted, StartActivityScans},
    garmin_endpoint::EndpointPublication,
    garmin_integration::GarminTransportOutcome,
    jobs::{GarminJobLease, JobStoreError, RecordedJobStatus},
};
use ga_domain::timestamp::StoredInstant;
use ga_garmin::{
    GarminError,
    legacy::{DailyEndpoint, LegacyRead},
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{OffsetDateTime, format_description::well_known::Iso8601};
use tokio::sync::Notify;
use uuid::Uuid;

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

    fn worker<'a>(&'a self, source: &'a Source) -> EndpointWorker<'a, Source> {
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
        fixture.worker(&success).run(&first).await?,
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
        fixture.worker(&limited).run(&rate).await,
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
        fixture.worker(&success).run(&preleased).await?,
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
            FOR EACH ROW WHEN (NEW.endpoint='activities') EXECUTE FUNCTION slow_activity_capture();",
    ).execute(&fixture.inspect).await?;
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
         DROP SEQUENCE synthetic_archiving_entered;",
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
