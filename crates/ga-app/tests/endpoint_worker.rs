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

use ga_app::garmin_worker::{EndpointSource, EndpointWork, EndpointWorker, EndpointWorkerError};
use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
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
use time::OffsetDateTime;
use uuid::Uuid;

const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct Source {
    calls: AtomicUsize,
    response: Result<Option<Vec<u8>>, GarminError>,
    delay: Duration,
}

impl Source {
    fn new(response: Result<Option<Vec<u8>>, GarminError>, delay: Duration) -> Self {
        Self {
            calls: AtomicUsize::new(0),
            response,
            delay,
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
        assert!(matches!(
            resource,
            LegacyRead::Daily(DailyEndpoint::HeartRate, _)
        ));
        assert_eq!(maximum_bytes, 65_536);
        self.calls.fetch_add(1, Ordering::Relaxed);
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

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and a synthetic Garmin transport"]
async fn publishes_late_response_and_pauses_already_leased_work() -> Result<(), Box<dyn Error>> {
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
