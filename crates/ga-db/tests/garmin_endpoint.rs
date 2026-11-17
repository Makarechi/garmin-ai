use std::{env, error::Error, io, str::FromStr, sync::Arc};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    garmin_endpoint::{
        CaptureEndpoint, EndpointCapture, EndpointError, EndpointPreflight, EndpointPublication,
        PublishEndpoint,
    },
    jobs::{FailureReason, GarminJobLease, JobStoreError},
    raw_capture::LegacyCapture,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Date, Duration, OffsetDateTime};
use uuid::Uuid;

const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct Fixture {
    database: Database,
    inspect: PgPool,
    archive: Arc<Archive>,
    clock: OffsetDateTime,
}

impl Fixture {
    async fn enqueue(
        &self,
        day: Date,
        at: OffsetDateTime,
        history: bool,
    ) -> Result<GarminJobLease, Box<dyn Error>> {
        let id = Uuid::new_v4();
        let mut payload = json!({"endpoint":"heart_rate","key":day.to_string()});
        if history {
            let key = window(day);
            payload["account"] = json!(ACCOUNT);
            payload["sync_window"] = json!(key);
            sqlx::query(
                "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            )
            .bind(key)
            .bind(
                json!({
                    "account": ACCOUNT, "status":"pending", "job_id":id.to_string(),
                    "endpoint":"heart_rate", "date": day.to_string(),
                })
                .to_string(),
            )
            .execute(&self.inspect)
            .await?;
        }
        sqlx::query(
            "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
             VALUES($1,'garmin_endpoint',$2::jsonb,$3,'pending',$4,0)",
        )
        .bind(id)
        .bind(payload.to_string())
        .bind(format!("synthetic:{id}"))
        .bind(at)
        .execute(&self.inspect)
        .await?;
        let lease = self
            .database
            .claim_garmin_job(at.try_into()?, at.date(), 60, false)
            .await?
            .ok_or_else(|| io::Error::other("Expected endpoint job"))?;
        assert_eq!(lease.id(), id);
        Ok(lease)
    }

    async fn capture(
        &self,
        day: Date,
        value: Value,
        at: StoredInstant,
    ) -> Result<Uuid, Box<dyn Error>> {
        let key = day.to_string();
        let source = self
            .database
            .capture_legacy_json(
                LegacyCapture {
                    authenticated_fingerprint: ACCOUNT,
                    endpoint: "heart_rate",
                    source_key: &key,
                    fetched_at: at,
                    body: serde_json::to_vec(&value)?,
                },
                Arc::clone(&self.archive),
                PutLimits {
                    max_bytes: 65536,
                    disk_reserve_bytes: 0,
                },
            )
            .await?;
        Ok(source.id)
    }

    async fn state(&self, key: &str) -> Result<Value, Box<dyn Error>> {
        let value: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(key)
            .fetch_one(&self.inspect)
            .await?;
        Ok(serde_json::from_str(&value)?)
    }

    fn request<'a>(
        &self,
        lease: &'a GarminJobLease,
        source: Uuid,
        at: StoredInstant,
    ) -> PublishEndpoint<'a> {
        PublishEndpoint {
            authenticated_fingerprint: ACCOUNT,
            lease,
            source_id: source,
            requested_at: at,
            timezone: "UTC",
            replacement: None,
        }
    }
}

fn window(day: Date) -> String {
    format!("syncwindow:{ACCOUNT}:heart_rate:{day}")
}

#[tokio::test]
#[ignore = "requires a disposable TimescaleDB with an isolated archive"]
async fn commits_observation_freshness_and_history_under_the_live_job() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected disposable TimescaleDB URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Database name must end in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(&actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Expected fixture configuration")?;
    let root = tempfile::tempdir()?;
    let archive = Arc::new(Archive::open(&root.path().canonicalize()?.join("raw"))?);
    let clock = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&inspect)
        .await?;
    let run = Fixture {
        database,
        inspect,
        archive,
        clock,
    };
    sqlx::query("INSERT INTO app_state(key,value) VALUES('account:garmin',jsonb_build_object('fingerprint',$1::text))")
        .bind(ACCOUNT).execute(&run.inspect).await?;
    let day = run.clock.date();
    let initial: StoredInstant = run.clock.try_into()?;
    let lease = run.enqueue(day, run.clock, true).await?;
    assert_eq!(
        run.database
            .preflight_garmin_endpoint(ACCOUNT, &lease, initial)
            .await?,
        EndpointPreflight::Fetch {
            endpoint: "heart_rate".into(),
            source_key: day.to_string()
        },
    );
    assert!(matches!(
        run.database
            .preflight_garmin_endpoint(&"b".repeat(64), &lease, initial)
            .await,
        Err(EndpointError::InvalidScope)
    ));
    let recent = initial.unix_microseconds() / 1_000 - 60_000;
    let body = serde_json::to_vec(&json!({"heartRateValues":[[recent,71]]}))?;
    let capture = || CaptureEndpoint {
        authenticated_fingerprint: ACCOUNT,
        lease: &lease,
        requested_at: initial,
        body: body.clone(),
    };
    let limits = PutLimits {
        max_bytes: 65536,
        disk_reserve_bytes: 0,
    };
    sqlx::raw_sql(
        "CREATE SEQUENCE synthetic_capture_started;
         CREATE FUNCTION expire_endpoint_during_capture() RETURNS trigger LANGUAGE plpgsql AS
          $$ BEGIN
            PERFORM nextval('synthetic_capture_started');
            UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'
             WHERE kind='garmin_endpoint' AND status='running';
            RETURN NEW;
          END $$;
         CREATE TRIGGER expire_endpoint_capture BEFORE INSERT ON source_payloads
         FOR EACH ROW EXECUTE FUNCTION expire_endpoint_during_capture();",
    )
    .execute(&run.inspect)
    .await?;
    assert!(matches!(
        run.database
            .capture_garmin_endpoint(capture(), Arc::clone(&run.archive), limits)
            .await,
        Err(EndpointError::Job(JobStoreError::LeaseLost))
    ));
    let after_capture: (i64, bool) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM source_payloads),
                (SELECT is_called FROM synthetic_capture_started)",
    )
    .fetch_one(&run.inspect)
    .await?;
    assert_eq!(after_capture, (0, true));
    sqlx::query("DROP TRIGGER expire_endpoint_capture ON source_payloads")
        .execute(&run.inspect)
        .await?;
    let EndpointCapture::Captured(first) = run
        .database
        .capture_garmin_endpoint(capture(), Arc::clone(&run.archive), limits)
        .await?
    else {
        return Err("Expected captured response".into());
    };
    assert!(first.newly_recorded);
    let EndpointCapture::Captured(repeated) = run
        .database
        .capture_garmin_endpoint(capture(), Arc::clone(&run.archive), limits)
        .await?
    else {
        return Err("Expected captured response on retry".into());
    };
    assert_eq!(repeated.id, first.id);
    assert_eq!(repeated.archive_key, first.archive_key);
    assert!(!repeated.newly_recorded);
    let original = first.id;
    let key = window(day);
    let freshness = format!("freshness:heart_rate:{day}");
    sqlx::raw_sql(
        "CREATE SEQUENCE synthetic_endpoint_attempts;
         CREATE FUNCTION stop_endpoint_window() RETURNS trigger LANGUAGE plpgsql AS
          $$ BEGIN IF NEW.value->>'status'='normalized' THEN
            PERFORM nextval('synthetic_endpoint_attempts');
            RAISE EXCEPTION 'synthetic late endpoint failure';
          END IF; RETURN NEW; END $$;
         CREATE TRIGGER stop_endpoint_window BEFORE UPDATE ON app_state
         FOR EACH ROW WHEN (starts_with(NEW.key,'syncwindow:'))
         EXECUTE FUNCTION stop_endpoint_window();",
    )
    .execute(&run.inspect)
    .await?;
    assert!(matches!(
        run.database
            .publish_garmin_endpoint(run.request(&lease, original, initial))
            .await,
        Err(EndpointError::Database)
    ));
    let facts: (i64, bool, String) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM measurements),
                (SELECT is_called FROM synthetic_endpoint_attempts),
                (SELECT status FROM source_payloads WHERE id=$1)",
    )
    .bind(original)
    .fetch_one(&run.inspect)
    .await?;
    assert_eq!(facts, (0, true, "pending".to_owned()));
    assert_eq!(run.state(&key).await?["status"], "pending");
    sqlx::query("DROP TRIGGER stop_endpoint_window ON app_state")
        .execute(&run.inspect)
        .await?;
    assert_eq!(
        run.database
            .publish_garmin_endpoint(run.request(&lease, original, initial))
            .await?,
        EndpointPublication::Published(DocumentStatus::Normalized)
    );
    assert_eq!(run.state(&key).await?["status"], "normalized");
    assert_eq!(run.state(&key).await?["source_ref"], original.to_string());
    assert_eq!(
        run.state(&freshness).await?["normalized_at"],
        initial.to_string()
    );

    let next = run.clock + Duration::seconds(1);
    let next_at: StoredInstant = next.try_into()?;
    let future = initial.unix_microseconds() / 1_000 + 120_000;
    let source = run
        .capture(day, json!({"heartRateValues":[[future,72]]}), next_at)
        .await?;
    let future_job = run.enqueue(day, next, true).await?;
    assert_eq!(
        run.database
            .publish_garmin_endpoint(run.request(&future_job, source, next_at))
            .await?,
        EndpointPublication::Published(DocumentStatus::Normalized)
    );
    let state = run.state(&freshness).await?;
    assert_eq!(state["success_at"], next_at.to_string());
    assert_eq!(state["normalized_at"], initial.to_string());

    let stale_raw = run
        .capture(day, json!({"heartRateValues":[[recent,55]]}), initial)
        .await?;
    let stale_job = run.enqueue(day, next + Duration::seconds(1), true).await?;
    assert_eq!(
        run.database
            .publish_garmin_endpoint(run.request(&stale_job, stale_raw, initial))
            .await?,
        EndpointPublication::Published(DocumentStatus::Stale)
    );
    let completion = run.state(&key).await?;
    assert_eq!(completion["status"], "superseded");
    assert_eq!(completion["source_ref"], source.to_string());
    assert_eq!(completion["source_status"], "normalized");
    assert_eq!(run.state(&freshness).await?, state);

    let retry = next + Duration::seconds(2);
    let retry_job = run.enqueue(day, retry, false).await?;
    run.database
        .finish_garmin_endpoint_fetch_error(
            ACCOUNT,
            &retry_job,
            retry.try_into()?,
            FailureReason::RuntimeError,
            None,
        )
        .await?;
    let state = run.state(&freshness).await?;
    assert_eq!(state["status"], "fetch_error");
    assert_eq!(state["success_at"], next_at.to_string());
    assert_eq!(state["normalized_at"], initial.to_string());

    let disabled = next + Duration::seconds(5);
    let disabled_job = run.enqueue(day, disabled, true).await?;
    sqlx::query(
        "INSERT INTO module_configs(id,owner_id,pack_key,tracking_enabled,collection_enabled,
         reminders_enabled,visible,llm_enabled,settings,revision)
         SELECT gen_random_uuid(),id,'wellbeing',false,false,false,false,false,'{}',1 FROM people",
    )
    .execute(&run.inspect)
    .await?;
    assert_eq!(
        run.database
            .preflight_garmin_endpoint(ACCOUNT, &disabled_job, disabled.try_into()?)
            .await?,
        EndpointPreflight::CollectionDisabled
    );
    assert_eq!(run.state(&key).await?["status"], "disabled");
    let status: String = sqlx::query_scalar("SELECT status FROM jobs WHERE id=$1")
        .bind(disabled_job.id())
        .fetch_one(&run.inspect)
        .await?;
    assert_eq!(status, "done");
    Ok(())
}
