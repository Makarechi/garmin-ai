use std::{env, error::Error, str::FromStr};

use ga_db::{
    Database,
    jobs::{JobStoreError, RecordedJobStatus},
};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses a freshly migrated disposable TimescaleDB"]
async fn schedules_and_recovers_daily_backups_with_the_garmin_wait_window()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected test database URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected dedicated test database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture database")?;
    let wall: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let anchor = wall
        .replace_hour(0)?
        .replace_minute(0)?
        .replace_second(0)?
        .replace_nanosecond(0)?;
    let at = |minutes| StoredInstant::try_from(anchor + Duration::minutes(minutes));
    let backup = db
        .schedule_daily_backup(at(0)?)
        .await?
        .ok_or("Missing daily backup")?;
    assert!(db.schedule_daily_backup(at(2)?).await?.is_none());

    let active: Uuid = sqlx::query_scalar(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES(gen_random_uuid(),'garmin_fit','{\"activity_id\":\"761\"}',
                  'synthetic-sync','pending',$1,0) RETURNING id",
    )
    .bind(anchor)
    .fetch_one(&pool)
    .await?;
    assert!(db.claim_daily_backup(at(29)?, 60).await?.is_none());
    let sync = db
        .claim_garmin_job(at(29)?, anchor.date(), 180, true)
        .await?
        .ok_or("Missing sync lease")?;
    assert_eq!(sync.id(), active);
    assert!(db.claim_daily_backup(at(30)?, 60).await?.is_none());
    assert!(
        db.claim_garmin_job(at(30)?, anchor.date(), 60, true)
            .await?
            .is_none()
    );

    let mut manual_storage = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104624)")
        .execute(&mut *manual_storage)
        .await?;
    assert!(db.claim_daily_backup(at(33)?, 60).await?.is_none());
    manual_storage.rollback().await?;

    let first = db
        .claim_daily_backup(at(33)?, 60)
        .await?
        .ok_or("Missing admitted backup after expiry")?;
    assert_eq!(first.date(), anchor.date());
    assert!(db.claim_daily_backup(at(33)?, 60).await?.is_none());
    assert_eq!(first.lease_duration().as_secs(), 60);
    assert!(db.renew_daily_backup(&first, at(33)?).await?);
    assert_eq!(
        db.defer_failed_daily_backup(&first, at(33)?).await?,
        RecordedJobStatus::Deferred
    );
    assert!(matches!(
        db.complete_daily_backup(&first, "/synthetic/old.enc", at(33)?)
            .await,
        Err(JobStoreError::LeaseLost),
    ));
    let saved: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='backup:last_success')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(!saved);

    sqlx::query("UPDATE jobs SET status='failed',attempts=8,completed_at=$2 WHERE id=$1")
        .bind(backup)
        .bind(anchor + Duration::minutes(35))
        .execute(&pool)
        .await?;
    assert!(db.schedule_daily_backup(at(94)?).await?.is_none());
    assert_eq!(db.schedule_daily_backup(at(95)?).await?, Some(backup));
    let retry: (i32, Option<OffsetDateTime>, Option<String>) =
        sqlx::query_as("SELECT attempts,completed_at,last_error FROM jobs WHERE id=$1")
            .bind(backup)
            .fetch_one(&pool)
            .await?;
    assert_eq!(retry, (0, None, None));

    // Work from a pre-upgrade queue keeps its day when it gets processed later.
    sqlx::query(
        "UPDATE jobs SET payload=payload-'date',run_at=clock_timestamp()-interval '1 hour'
         WHERE id=$1",
    )
    .bind(backup)
    .execute(&pool)
    .await?;
    sqlx::query("UPDATE jobs SET status='done',lease_until=NULL,lease_token=NULL WHERE id=$1")
        .bind(active)
        .execute(&pool)
        .await?;
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(now)?;
    let retry = db
        .claim_daily_backup(now, 60)
        .await?
        .ok_or("Missing resumed backup")?;
    assert_eq!(retry.date(), anchor.date());
    db.complete_daily_backup(&retry, "/synthetic/recovered.enc", now)
        .await?;
    let published: (String, String) = sqlx::query_as(
        "SELECT job.status, saved.value->>'path'
         FROM jobs job, app_state saved WHERE job.id=$1 AND saved.key='backup:last_success'",
    )
    .bind(backup)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        published,
        ("done".into(), "/synthetic/recovered.enc".into())
    );
    assert!(db.schedule_daily_backup(at(96)?).await?.is_none());
    Ok(())
}
