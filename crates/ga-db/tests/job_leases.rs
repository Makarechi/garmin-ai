use std::{
    env,
    error::Error,
    io,
    process::Stdio,
    str::FromStr,
    time::{Duration, Instant},
};

use ga_db::{
    Database, ReadinessError,
    jobs::{FailureReason, InitiativeOutcome, JobStoreError, RecordedJobStatus},
};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, PgConnection, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
    time::timeout,
};
use uuid::Uuid;

type JobState = (String, i32, Option<String>, Option<OffsetDateTime>);

fn at(seconds: i64) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::from_unix_microseconds(
        1_800_000_000_000_000 + seconds * 1_000_000,
    )?)
}

async fn state(database: &PgPool, id: Uuid) -> Result<JobState, sqlx::Error> {
    sqlx::query_as("SELECT status, attempts, last_error, completed_at FROM jobs WHERE id = $1")
        .bind(id)
        .fetch_one(database)
        .await
}

fn required<T>(value: Option<T>) -> Result<T, io::Error> {
    value.ok_or_else(|| io::Error::other("Expected a job from the disposable fixture"))
}

#[tokio::test]
#[ignore = "uses a freshly migrated, disposable TimescaleDB"]
async fn real_postgres_initiative_leases_survive_restart_and_reject_old_owners()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url.strip_prefix("postgresql+psycopg://").ok_or_else(|| {
        io::Error::other("GA_TEST_DATABASE_URL must target a PostgreSQL test database")
    })?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let database_name = options
        .get_database()
        .filter(|name| name.ends_with("_test"));
    let name = required(database_name)?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(current, name);
    let time_scale: Option<String> =
        sqlx::query_scalar("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            .fetch_optional(&inspect)
            .await?;
    assert!(time_scale.is_some());
    let existing: i64 = sqlx::query_scalar("SELECT count(*) FROM jobs")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(existing, 0, "Use an empty job fixture");
    let database = Database::connect_lazy(&url)
        .map_err(|_| io::Error::other("Database configuration failed"))?;
    verify_process_death(&inspect, &database).await?;
    verify_publication_expiry(&inspect, &database).await?;
    let now = at(0)?;

    let (one, two) = tokio::join!(
        database.enqueue_initiative("synthetic-lease:dedup", now),
        database.enqueue_initiative("synthetic-lease:dedup", now)
    );
    let ids = [one?, two?];
    assert_eq!(ids.iter().flatten().count(), 1);
    let job_id = required(ids.into_iter().flatten().next())?;
    assert!(matches!(
        database.claim_local_initiative(now, 0).await,
        Err(JobStoreError::InvalidLeaseDuration)
    ));
    let (one, two) = tokio::join!(
        database.claim_local_initiative(now, 30),
        database.claim_local_initiative(now, 30)
    );
    let leases = [one?, two?];
    assert_eq!(leases.iter().flatten().count(), 1);
    let original = required(leases.into_iter().flatten().next())?;
    assert_eq!((original.id(), original.attempt()), (job_id, 1));
    assert!(database.renew_initiative(&original, at(10)?).await?);
    assert!(database.renew_initiative(&original, at(5)?).await?);
    assert_eq!(
        sqlx::query_scalar::<_, OffsetDateTime>("SELECT lease_until FROM jobs WHERE id = $1")
            .bind(job_id)
            .fetch_one(&inspect)
            .await?,
        OffsetDateTime::from(at(40)?)
    );
    assert!(!database.renew_initiative(&original, at(40)?).await?);
    assert!(
        database
            .claim_local_initiative(at(40)?, 30)
            .await?
            .is_none()
    );

    drop(database);
    let restored = Database::connect_lazy(&url)
        .map_err(|_| io::Error::other("Database configuration failed"))?;
    let current_lease = required(restored.claim_local_initiative(at(41)?, 30).await?)?;
    assert_eq!((current_lease.id(), current_lease.attempt()), (job_id, 2));
    assert!(!restored.renew_initiative(&original, at(42)?).await?);
    assert!(matches!(
        restored
            .finish_initiative(&original, at(42)?, InitiativeOutcome::Completed)
            .await,
        Err(JobStoreError::LeaseLost)
    ));
    assert_eq!(
        restored
            .finish_initiative(&current_lease, at(43)?, InitiativeOutcome::Completed)
            .await?,
        RecordedJobStatus::Completed
    );
    assert_eq!(
        state(&inspect, job_id).await?,
        ("done".to_owned(), 2, None, Some(at(43)?.into()))
    );

    let deferred_id = required(
        restored
            .enqueue_initiative("synthetic-lease:deferred", at(50)?)
            .await?,
    )?;
    assert!(
        restored
            .claim_local_initiative(at(49)?, 30)
            .await?
            .is_none()
    );
    let deferred = required(restored.claim_local_initiative(at(50)?, 30).await?)?;
    assert_eq!(deferred.id(), deferred_id);
    assert_eq!(
        restored
            .finish_initiative(
                &deferred,
                at(51)?,
                InitiativeOutcome::Failed {
                    reason: FailureReason::DiaryDeferred,
                    retry_at: Some(at(90)?),
                }
            )
            .await?,
        RecordedJobStatus::Deferred
    );
    assert_eq!(
        state(&inspect, deferred_id).await?,
        (
            "pending".to_owned(),
            0,
            Some("DiaryDeferred".to_owned()),
            None
        )
    );
    assert!(
        restored
            .claim_local_initiative(at(89)?, 30)
            .await?
            .is_none()
    );
    let retried = required(restored.claim_local_initiative(at(90)?, 30).await?)?;
    assert_eq!((retried.id(), retried.attempt()), (deferred_id, 1));
    assert_eq!(
        restored
            .finish_initiative(
                &retried,
                at(91)?,
                InitiativeOutcome::Failed {
                    reason: FailureReason::ProviderRequestInvalid,
                    retry_at: Some(at(100)?),
                }
            )
            .await?,
        RecordedJobStatus::Failed
    );
    assert_eq!(
        state(&inspect, deferred_id).await?,
        (
            "failed".to_owned(),
            1,
            Some("ProviderRequestInvalid".to_owned()),
            Some(at(91)?.into())
        )
    );

    let default_retry = required(
        restored
            .enqueue_initiative("synthetic-lease:default-retry", at(100)?)
            .await?,
    )?;
    let default_lease = required(restored.claim_local_initiative(at(100)?, 30).await?)?;
    assert_eq!(default_lease.id(), default_retry);
    assert_eq!(
        restored
            .finish_initiative(
                &default_lease,
                at(101)?,
                InitiativeOutcome::Failed {
                    reason: FailureReason::RuntimeError,
                    retry_at: None,
                },
            )
            .await?,
        RecordedJobStatus::Deferred
    );
    let run_at = sqlx::query_scalar::<_, OffsetDateTime>("SELECT run_at FROM jobs WHERE id = $1")
        .bind(default_retry)
        .fetch_one(&inspect)
        .await?;
    assert!((OffsetDateTime::from(at(131)?)..=at(141)?.into()).contains(&run_at));

    let exhausted = required(
        restored
            .enqueue_initiative("synthetic-lease:exhausted", at(120)?)
            .await?,
    )?;
    sqlx::query("UPDATE jobs SET attempts = 8 WHERE id = $1")
        .bind(exhausted)
        .execute(&inspect)
        .await?;
    assert!(
        restored
            .claim_local_initiative(at(120)?, 30)
            .await?
            .is_none()
    );
    assert_eq!(
        state(&inspect, exhausted).await?,
        (
            "failed".to_owned(),
            8,
            Some("RetryLimitExceeded".to_owned()),
            Some(at(120)?.into())
        )
    );

    let mut blocked = inspect.begin().await?;
    sqlx::query("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *blocked)
        .await?;
    let one_store = restored.clone();
    let two_store = restored.clone();
    let one = tokio::spawn(async move {
        one_store
            .enqueue_initiative("synthetic-lease:blocked-1", now)
            .await
    });
    let two = tokio::spawn(async move {
        two_store
            .enqueue_initiative("synthetic-lease:blocked-2", now)
            .await
    });
    let start = Instant::now();
    loop {
        let waiting: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM pg_stat_activity
             WHERE datname = current_database() AND application_name = 'garmin-ai-rust'
               AND wait_event_type = 'Lock' AND query LIKE 'INSERT INTO jobs%'",
        )
        .fetch_one(&inspect)
        .await?;
        if waiting == 2 {
            break;
        }
        if start.elapsed() > Duration::from_millis(500) {
            return Err(
                io::Error::other("Both work connections did not reach the table lock").into(),
            );
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(restored.readiness().await, Ok(()));
    blocked.rollback().await?;
    assert!(one.await??.is_some());
    assert!(two.await??.is_some());

    let mut storage = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104622)")
        .execute(&mut *storage)
        .await?;
    assert!(matches!(
        restored
            .enqueue_initiative("synthetic-lease:erasure", now)
            .await,
        Err(JobStoreError::Storage(ReadinessError::Unavailable))
    ));
    storage.rollback().await?;
    Ok(())
}

const PUBLICATION_KEY: &str = "synthetic:lease-guarded-publication";
const CHILD_JOB_ENV: &str = "GA_TEST_JOB_PUBLICATION_CHILD_ID";
const STAGED_MESSAGE: &str = "synthetic publication staged";

fn real_now() -> Result<StoredInstant, Box<dyn Error>> {
    let microseconds = OffsetDateTime::now_utc()
        .unix_timestamp_nanos()
        .div_euclid(1_000);
    Ok(StoredInstant::from_unix_microseconds(i64::try_from(
        microseconds,
    )?)?)
}

async fn publish_marker(connection: &mut PgConnection) -> Result<(), JobStoreError> {
    sqlx::query(
        "INSERT INTO app_state (key, value) VALUES ($1, '{\"status\":\"synthetic\"}'::jsonb)",
    )
    .bind(PUBLICATION_KEY)
    .execute(connection)
    .await?;
    Ok(())
}

async fn marker_exists(inspect: &PgPool) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_state WHERE key = $1)")
        .bind(PUBLICATION_KEY)
        .fetch_one(inspect)
        .await
}

async fn verify_process_death(inspect: &PgPool, database: &Database) -> Result<(), Box<dyn Error>> {
    let id = required(
        database
            .enqueue_initiative("synthetic-publication:kill", real_now()?)
            .await?,
    )?;
    let mut child = Command::new(env::current_exe()?)
        .args([
            "--exact",
            "synthetic_child_publication_waits_for_kill",
            "--ignored",
            "--nocapture",
        ])
        .env(CHILD_JOB_ENV, id.to_string())
        .stdout(Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let mut lines = BufReader::new(required(child.stdout.take())?).lines();
    timeout(Duration::from_secs(5), async {
        while let Some(line) = lines.next_line().await? {
            if line == STAGED_MESSAGE {
                return Ok(());
            }
        }
        Err(io::Error::other(
            "Child exited before staging its synthetic write",
        ))
    })
    .await??;
    assert!(!marker_exists(inspect).await?);
    assert_eq!(database.readiness().await, Ok(()));
    child.kill().await?;
    assert!(!marker_exists(inspect).await?);
    assert_eq!(state(inspect, id).await?.0, "running");

    // Leave enough real time for the three-second lease to expire after the process exits.
    tokio::time::sleep(Duration::from_secs(3)).await;
    let current = required(database.claim_local_initiative(real_now()?, 30).await?)?;
    assert_eq!((current.id(), current.attempt()), (id, 2));
    assert!(matches!(
        database
            .publish_initiative(&current, async |connection| {
                publish_marker(connection).await?;
                Err(JobStoreError::Database)
            })
            .await,
        Err(JobStoreError::Database)
    ));
    assert!(!marker_exists(inspect).await?);
    database
        .publish_initiative(&current, async |connection| {
            publish_marker(connection).await
        })
        .await?;
    assert!(marker_exists(inspect).await?);
    assert_eq!(state(inspect, id).await?.0, "done");
    assert!(matches!(
        database
            .publish_initiative(&current, async |_connection| {
                Err(JobStoreError::Database)
            })
            .await,
        Err(JobStoreError::LeaseLost)
    ));
    Ok(())
}

async fn verify_publication_expiry(
    inspect: &PgPool,
    database: &Database,
) -> Result<(), Box<dyn Error>> {
    let now = real_now()?.unix_microseconds();
    let two_seconds_ago = StoredInstant::from_unix_microseconds(now - 2_000_000)?;
    let half_second_ago = StoredInstant::from_unix_microseconds(now - 500_000)?;
    let id = required(
        database
            .enqueue_initiative("synthetic-publication:expiry", two_seconds_ago)
            .await?,
    )?;
    let claim = required(database.claim_local_initiative(half_second_ago, 1).await?)?;
    assert_eq!(claim.id(), id);
    let result = database
        .publish_initiative(&claim, async |connection| {
            sqlx::query("DELETE FROM app_state WHERE key = $1")
                .bind(PUBLICATION_KEY)
                .execute(connection)
                .await?;
            tokio::time::sleep(Duration::from_millis(750)).await;
            Ok(())
        })
        .await;
    assert!(matches!(result, Err(JobStoreError::LeaseLost)));
    assert!(marker_exists(inspect).await?);
    assert_eq!(state(inspect, id).await?.0, "running");
    let replacement = required(database.claim_local_initiative(real_now()?, 30).await?)?;
    assert_eq!((replacement.id(), replacement.attempt()), (id, 2));
    database
        .publish_initiative(&replacement, async |_connection| Ok(()))
        .await?;
    Ok(())
}

#[tokio::test]
#[ignore = "child process used by the real database fixture test"]
async fn synthetic_child_publication_waits_for_kill() -> Result<(), Box<dyn Error>> {
    let Ok(id) = env::var(CHILD_JOB_ENV) else {
        return Ok(());
    };
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Not a PostgreSQL fixture"))?;
    let options = PgConnectOptions::from_str(&format!("postgresql://{address}"))?;
    required(
        options
            .get_database()
            .filter(|name| name.ends_with("_test")),
    )?;
    let database = Database::connect_lazy(&url)
        .map_err(|_| io::Error::other("Invalid database configuration"))?;
    let claim = required(database.claim_local_initiative(real_now()?, 3).await?)?;
    assert_eq!(claim.id(), Uuid::parse_str(&id)?);
    database
        .publish_initiative(&claim, async |connection| {
            publish_marker(connection).await?;
            println!("{STAGED_MESSAGE}");
            std::future::pending().await
        })
        .await?;
    Err(io::Error::other("Synthetic child unexpectedly completed").into())
}
