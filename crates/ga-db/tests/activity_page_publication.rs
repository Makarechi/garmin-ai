use std::{env, error::Error, io, str::FromStr, sync::Arc};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::DocumentStatus,
    activity_scans::{
        ActivityPagePreflight, ActivityPageResult, ActivityScanError, ActivityScansStarted,
        PublishActivityPage, StartActivityScans,
    },
    jobs::{FailureReason, GarminJobLease, JobStoreError},
    raw_capture::LegacyCapture,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const ZONE: &str = "Europe/Bratislava";

fn required<T>(value: Option<T>, label: &str) -> Result<T, io::Error> {
    value.ok_or_else(|| io::Error::other(label))
}

async fn state(db: &PgPool, key: &str) -> Result<Value, Box<dyn Error>> {
    let text: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
        .bind(key)
        .fetch_one(db)
        .await?;
    Ok(serde_json::from_str(&text)?)
}

async fn claim(
    database: &Database,
    inspect: &PgPool,
    now: OffsetDateTime,
) -> Result<GarminJobLease, Box<dyn Error>> {
    let lease = required(
        database
            .claim_garmin_job(now.try_into()?, now.date(), 60, false)
            .await?,
        "expected activity page lease",
    )?;
    let kind: String = sqlx::query_scalar("SELECT kind FROM jobs WHERE id=$1")
        .bind(lease.id())
        .fetch_one(inspect)
        .await?;
    assert_eq!(kind, "garmin_activities");
    Ok(lease)
}

#[tokio::test]
#[ignore = "uses a freshly migrated disposable TimescaleDB and synthetic archive"]
async fn page_results_cursor_children_and_job_share_one_commit() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let host = required(
        url.strip_prefix("postgresql+psycopg://"),
        "expected disposable PostgreSQL fixture",
    )?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{host}"))?.disable_statement_logging();
    let name = required(
        options
            .get_database()
            .filter(|name| name.ends_with("_test")),
        "fixture database name must end with _test",
    )?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(name, actual);
    let database = Database::connect_lazy(&url).map_err(|_| "invalid fixture connection")?;
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&inspect)
        .await?;
    let now_text = StoredInstant::try_from(now)?.to_string();

    sqlx::query(
        "INSERT INTO app_state(key,value)
         VALUES('account:garmin',jsonb_build_object('fingerprint',$1::text))",
    )
    .bind(ACCOUNT)
    .execute(&inspect)
    .await?;
    let key = format!("activity_scan:{ACCOUNT}:recent");
    assert_eq!(
        database
            .start_activity_scans(StartActivityScans {
                authenticated_fingerprint: ACCOUNT,
                timezone: ZONE,
                history_days: 0,
                now: now.try_into()?,
            })
            .await?,
        ActivityScansStarted::Scheduled { pages: 1 }
    );
    let current = claim(&database, &inspect, now).await?;
    assert_eq!(
        database
            .preflight_activity_page(ACCOUNT, &current, now.try_into()?)
            .await?,
        ActivityPagePreflight::Fetch { offset: 0 }
    );
    let root = tempfile::tempdir()?;
    let archive = Arc::new(Archive::open(&root.path().canonicalize()?.join("raw"))?);
    let capture = async |key: &str, value: Value, seconds: i64| -> Result<Uuid, Box<dyn Error>> {
        let captured = database
            .capture_legacy_json(
                LegacyCapture {
                    authenticated_fingerprint: ACCOUNT,
                    endpoint: "activities",
                    source_key: key,
                    fetched_at: (now + Duration::seconds(seconds)).try_into()?,
                    body: serde_json::to_vec(&value)?,
                },
                Arc::clone(&archive),
                PutLimits {
                    max_bytes: 65_536,
                    disk_reserve_bytes: 0,
                },
            )
            .await?;
        Ok(captured.id)
    };
    let values: Vec<_> = (0..100)
        .map(|index| {
            json!({"activityId":format!("synthetic-{index:03}"),
                  "startTimeGMT": now_text, "duration": 30})
        })
        .collect();
    let first = capture("page:0", json!(values), 0).await?;
    let first_clock: StoredInstant = now.try_into()?;
    let first_request = || PublishActivityPage {
        authenticated_fingerprint: ACCOUNT,
        lease: &current,
        source_id: first,
        requested_at: first_clock,
        timezone: ZONE,
    };
    sqlx::raw_sql(
        "CREATE SEQUENCE synthetic_cursor_seen;
         CREATE FUNCTION stop_synthetic_cursor() RETURNS trigger LANGUAGE plpgsql AS
          $$ BEGIN IF NEW.value->>'offset'='80' THEN
                    PERFORM nextval('synthetic_cursor_seen');
                    RAISE EXCEPTION 'synthetic final cursor failure';
                 END IF; RETURN NEW; END $$;
         CREATE TRIGGER stop_synthetic_cursor BEFORE UPDATE ON app_state
         FOR EACH ROW WHEN (starts_with(NEW.key,'activity_scan:'))
         EXECUTE FUNCTION stop_synthetic_cursor();",
    )
    .execute(&inspect)
    .await?;
    assert!(matches!(
        database.publish_activity_page(first_request()).await,
        Err(ActivityScanError::Database)
    ));
    let reached_cursor: bool = sqlx::query_scalar("SELECT is_called FROM synthetic_cursor_seen")
        .fetch_one(&inspect)
        .await?;
    assert!(reached_cursor);
    let counts: (i64, i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM activities),
                (SELECT count(*) FROM jobs),
                (SELECT count(*) FROM app_state WHERE key='freshness:activities:page:0')",
    )
    .fetch_one(&inspect)
    .await?;
    assert_eq!(counts, (0, 1, 0));
    assert_eq!(state(&inspect, &key).await?["pages"], 0);
    let raw: String = sqlx::query_scalar("SELECT status FROM source_payloads WHERE id=$1")
        .bind(first)
        .fetch_one(&inspect)
        .await?;
    assert_eq!(raw, "pending");
    sqlx::query("DROP TRIGGER stop_synthetic_cursor ON app_state")
        .execute(&inspect)
        .await?;

    assert_eq!(
        database.publish_activity_page(first_request()).await?,
        ActivityPageResult::Published(DocumentStatus::Normalized)
    );
    let counts: (i64, i64, i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM activities),
                (SELECT count(*) FROM jobs WHERE kind='garmin_endpoint'),
                (SELECT count(*) FROM jobs WHERE kind='garmin_fit'),
                (SELECT count(*) FROM jobs WHERE kind='garmin_activities' AND status='pending')",
    )
    .fetch_one(&inspect)
    .await?;
    assert_eq!(counts, (100, 1200, 100, 1));
    let saved = state(&inspect, &key).await?;
    assert_eq!(saved["offset"], 80);
    assert_eq!(saved["pages"], 1);
    assert_eq!(saved["anchor"][0], "synthetic-080");
    assert_eq!(saved["anchor"][19], "synthetic-099");
    let job_state: String = sqlx::query_scalar("SELECT status FROM jobs WHERE id=$1")
        .bind(current.id())
        .fetch_one(&inspect)
        .await?;
    assert_eq!(job_state, "done");
    let example: (String, i32, i32) = sqlx::query_as(
        "SELECT max(payload->>'account'),
                min(extract(epoch from run_at-$1::timestamptz)::int),
                max(extract(epoch from run_at-$1::timestamptz)::int)
         FROM jobs WHERE kind='garmin_endpoint' AND payload->>'endpoint'='activity_details'
                   AND payload->>'key'='synthetic-000'",
    )
    .bind(now)
    .fetch_one(&inspect)
    .await?;
    assert_eq!(example, (ACCOUNT.to_owned(), 0, 1200));

    // Isolate the scan lane for the remaining fixture, leaving all dedup keys intact.
    sqlx::query("UPDATE jobs SET run_at=run_at+interval '2 days' WHERE kind!='garmin_activities'")
        .execute(&inspect)
        .await?;
    let second_clock = now + Duration::seconds(1);
    let next = claim(&database, &inspect, second_clock).await?;
    assert_eq!(
        database
            .preflight_activity_page(ACCOUNT, &next, second_clock.try_into()?)
            .await?,
        ActivityPagePreflight::Fetch { offset: 80 }
    );
    let shifted = capture("page:80", json!([values[0]]), 1).await?;
    assert_eq!(
        database
            .publish_activity_page(PublishActivityPage {
                authenticated_fingerprint: ACCOUNT,
                lease: &next,
                source_id: shifted,
                requested_at: second_clock.try_into()?,
                timezone: ZONE,
            })
            .await?,
        ActivityPageResult::Published(DocumentStatus::Normalized)
    );
    let saved = state(&inspect, &key).await?;
    assert_eq!(saved["round"], 1);
    assert_eq!(saved["offset"], 0);
    assert_eq!(saved["anchor"], json!([]));
    assert_eq!(saved["pages"], 2);
    let children: i64 =
        sqlx::query_scalar("SELECT count(*) FROM jobs WHERE kind!='garmin_activities'")
            .fetch_one(&inspect)
            .await?;
    assert_eq!(children, 1300);

    let third_clock = second_clock + Duration::seconds(1);
    let third = claim(&database, &inspect, third_clock).await?;
    let late = capture("page:0", json!([values[1]]), 2).await?;
    sqlx::raw_sql(
        "CREATE SEQUENCE synthetic_lease_seen;
         CREATE FUNCTION close_synthetic_lease() RETURNS trigger LANGUAGE plpgsql AS
          $$ BEGIN IF NEW.value->>'status'='complete' THEN
                    PERFORM nextval('synthetic_lease_seen');
                    UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'
                    WHERE id=(OLD.value->>'job_id')::uuid;
                 END IF; RETURN NEW; END $$;
         CREATE TRIGGER close_synthetic_lease BEFORE UPDATE ON app_state
         FOR EACH ROW WHEN (starts_with(NEW.key,'activity_scan:'))
         EXECUTE FUNCTION close_synthetic_lease();",
    )
    .execute(&inspect)
    .await?;
    assert!(matches!(
        database
            .publish_activity_page(PublishActivityPage {
                authenticated_fingerprint: ACCOUNT,
                lease: &third,
                source_id: late,
                requested_at: third_clock.try_into()?,
                timezone: ZONE,
            })
            .await,
        Err(ActivityScanError::Job(JobStoreError::LeaseLost))
    ));
    let reached_lease_check: bool =
        sqlx::query_scalar("SELECT is_called FROM synthetic_lease_seen")
            .fetch_one(&inspect)
            .await?;
    assert!(reached_lease_check);
    assert_eq!(state(&inspect, &key).await?["pages"], 2);
    assert_eq!(
        state(&inspect, "freshness:activities:page:0").await?["source_ref"],
        first.to_string()
    );
    let late_status: String = sqlx::query_scalar("SELECT status FROM source_payloads WHERE id=$1")
        .bind(late)
        .fetch_one(&inspect)
        .await?;
    assert_eq!(late_status, "pending");
    sqlx::raw_sql(
        "DROP TRIGGER close_synthetic_lease ON app_state;
         UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'
         WHERE status='running' AND kind='garmin_activities';",
    )
    .execute(&inspect)
    .await?;
    let retry = claim(&database, &inspect, third_clock).await?;
    assert_eq!(retry.id(), third.id());
    assert_eq!(retry.attempt(), 2);
    let malformed = capture("page:0", json!({"unexpected": true}), 2).await?;
    assert_eq!(
        database
            .publish_activity_page(PublishActivityPage {
                authenticated_fingerprint: ACCOUNT,
                lease: &retry,
                source_id: malformed,
                requested_at: third_clock.try_into()?,
                timezone: ZONE,
            })
            .await?,
        ActivityPageResult::Retry(DocumentStatus::Error)
    );
    assert_eq!(state(&inspect, &key).await?["pages"], 2);
    let freshness = state(&inspect, "freshness:activities:page:0").await?;
    assert_eq!(freshness["status"], "error");
    assert_eq!(freshness["normalized_at"], now_text);
    let fourth_clock = now + Duration::seconds(300);
    let fourth = claim(&database, &inspect, fourth_clock).await?;
    assert_eq!(fourth.id(), retry.id());
    database
        .finish_activity_page_fetch_error(
            ACCOUNT,
            &fourth,
            fourth_clock.try_into()?,
            FailureReason::RuntimeError,
            None,
        )
        .await?;
    assert_eq!(state(&inspect, &key).await?["pages"], 2);
    let failure = state(&inspect, "freshness:activities:page:0").await?;
    assert_eq!(failure["status"], "fetch_error");
    assert_eq!(failure["success_at"], freshness["success_at"]);
    assert_eq!(failure["normalized_at"], now_text);
    assert!(failure["source_ref"].is_null());
    Ok(())
}
