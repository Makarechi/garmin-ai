use std::{env, error::Error, str::FromStr};

use ga_db::{
    Database,
    jobs::{
        FailureReason, JobOutcome, JobStoreError, RecordedJobStatus,
        telegram_ack::{TelegramAckTarget, enqueue_telegram_ack},
    },
};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "requires a disposable TimescaleDB with the pinned Python schema"]
async fn keeps_callback_work_independent_and_reclaims_after_restart() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected synthetic URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid synthetic connection")?;
    let now: StoredInstant = "2026-11-17T12:00:00Z".parse()?;
    let primary = TelegramAckTarget {
        owner_id,
        instance_id: "primary",
        confirmed_chat: Some(42),
    };
    let secondary = TelegramAckTarget {
        instance_id: "secondary",
        ..primary
    };
    let foreign = TelegramAckTarget {
        owner_id: Uuid::from_u128(19),
        ..primary
    };

    let mut txn = pool.begin().await?;
    assert!(enqueue_telegram_ack(&mut txn, primary, 41, "synthetic-lost", now).await?);
    txn.rollback().await?;
    assert!(database.claim_telegram_ack(primary, now).await?.is_none());

    let mut txn = pool.begin().await?;
    assert!(matches!(
        enqueue_telegram_ack(&mut txn, primary, 40, "", now).await,
        Err(JobStoreError::InvalidRecordedState)
    ));
    assert!(enqueue_telegram_ack(&mut txn, primary, 42, "synthetic-primary", now).await?);
    assert!(!enqueue_telegram_ack(&mut txn, primary, 42, "ignored-duplicate", now).await?);
    assert!(enqueue_telegram_ack(&mut txn, secondary, 42, "synthetic-secondary", now).await?);
    sqlx::query("INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
            VALUES(gen_random_uuid(),'telegram_ack','{\"update_id\":42}','telegram:ack:42','pending','2026-11-17T12:00:00Z',0)")
        .execute(&mut *txn).await?;
    txn.commit().await?;
    assert!(database.claim_telegram_ack(foreign, now).await?.is_none());

    let (one, two) = tokio::join!(
        database.claim_telegram_ack(primary, now),
        database.claim_telegram_ack(primary, now)
    );
    let ((Some(first), None) | (None, Some(first))) = (one?, two?) else {
        return Err("Expected exactly one claimant for the primary acknowledgement".into());
    };
    assert_eq!(first.callback_id(), "synthetic-primary");

    let other = database
        .claim_telegram_ack(secondary, now)
        .await?
        .ok_or("Missing separate bot acknowledgement")?;
    assert_eq!(other.callback_id(), "synthetic-secondary");
    assert_eq!(
        database
            .finish_telegram_ack(&other, now, JobOutcome::Completed)
            .await?,
        RecordedJobStatus::Completed
    );

    let restarted = Database::connect_lazy(&url).map_err(|_| "Invalid restarted connection")?;
    let later = seconds_after(now, 16)?;
    let replacement = restarted
        .claim_telegram_ack(primary, later)
        .await?
        .ok_or("Missing recovered acknowledgement")?;
    assert_eq!(replacement.callback_id(), first.callback_id());
    assert!(matches!(
        database
            .finish_telegram_ack(&first, later, JobOutcome::Completed)
            .await,
        Err(JobStoreError::LeaseLost)
    ));
    let retry_at = seconds_after(now, 32)?;
    assert_eq!(
        restarted
            .finish_telegram_ack(
                &replacement,
                later,
                JobOutcome::Failed {
                    reason: FailureReason::RuntimeError,
                    retry_at: Some(retry_at),
                },
            )
            .await?,
        RecordedJobStatus::Deferred
    );
    assert!(
        restarted
            .claim_telegram_ack(primary, seconds_after(now, 31)?)
            .await?
            .is_none()
    );
    let last = restarted
        .claim_telegram_ack(primary, retry_at)
        .await?
        .ok_or("Missing deferred acknowledgement")?;
    assert_eq!(
        restarted
            .finish_telegram_ack(&last, retry_at, JobOutcome::Completed)
            .await?,
        RecordedJobStatus::Completed
    );
    assert!(
        restarted
            .claim_telegram_ack(primary, retry_at)
            .await?
            .is_none()
    );
    let legacy: (String, i32) =
        sqlx::query_as("SELECT status,attempts FROM jobs WHERE kind='telegram_ack'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(legacy, ("pending".to_owned(), 0));
    println!(
        "Verified transactional acknowledgements, both bots, competing workers, restart, and timed retry"
    );
    Ok(())
}

fn seconds_after(now: StoredInstant, seconds: i64) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::from_unix_microseconds(
        now.unix_microseconds() + seconds * 1_000_000,
    )?)
}
