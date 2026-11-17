use std::{env, error::Error, io, str::FromStr};

use ga_db::{
    Database,
    jobs::{FailureReason, GarminJobKind, JobOutcome, JobStoreError, RecordedJobStatus},
};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Date, Month, OffsetDateTime};
use uuid::Uuid;

const FIT: &str = r#"{"activity_id":"42"}"#;

fn at(seconds: i64) -> Result<StoredInstant, Box<dyn Error>> {
    let start = StoredInstant::from_str("2027-01-15T12:00:00+00:00")?;
    Ok(StoredInstant::from_unix_microseconds(
        start.unix_microseconds() + 1_000_000 * seconds,
    )?)
}

#[tokio::test]
#[ignore = "uses a migrated disposable TimescaleDB fixture"]
async fn garmin_claims_preserve_freshness_priority_cooldown_backup_and_lease_recovery()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, expected);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let restart = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let today = Date::from_calendar_date(2027, Month::January, 15)?;

    let daily = add(
        &inspect,
        "garmin_endpoint",
        "live:synthetic-daily",
        -1500,
        r#"{"endpoint":"daily","key":"2027-01-15"}"#,
    )
    .await?;
    let fit = add(&inspect, "garmin_fit", "fit:synthetic", -120, FIT).await?;
    let activity = add(
        &inspect,
        "garmin_activities",
        "activity:synthetic",
        -30,
        "{}",
    )
    .await?;
    let stress = add(
        &inspect,
        "garmin_endpoint",
        "history:synthetic-stress",
        -3000,
        r#"{"endpoint":"stress","key":"2027-01-14","backfill":true}"#,
    )
    .await?;
    let hr = add(
        &inspect,
        "garmin_endpoint",
        "history:synthetic-hr",
        -3600,
        r#"{"endpoint":"heart_rate","key":"2027-01-14","backfill":true}"#,
    )
    .await?;
    let backup = add(&inspect, "backup", "backup:synthetic-overdue", -1801, "{}").await?;

    let mut other_claim = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104624)")
        .execute(&mut *other_claim)
        .await?;
    assert!(
        db.claim_garmin_job(at(0)?, today, 60, false)
            .await?
            .is_none()
    );
    other_claim.rollback().await?;
    assert!(
        db.claim_garmin_job(at(0)?, today, 60, true)
            .await?
            .is_none()
    );
    sqlx::query("UPDATE jobs SET status = 'done' WHERE id = $1")
        .bind(backup)
        .execute(&inspect)
        .await?;

    write_gate(&inspect, "reauth_required", None).await?;
    assert!(
        db.claim_garmin_job(at(0)?, today, 60, true)
            .await?
            .is_none()
    );
    write_gate(&inspect, "rate_limited", Some(at(2)?)).await?;
    assert!(
        db.claim_garmin_job(at(1)?, today, 60, true)
            .await?
            .is_none()
    );

    let first = db
        .claim_garmin_job(at(2)?, today, 60, true)
        .await?
        .ok_or("First lease missing")?;
    assert_eq!(first.id(), fit);
    assert_eq!(first.kind(), GarminJobKind::Fit);
    assert!(first.payload_json().contains("\"42\""));
    assert_eq!(first.attempt(), 1);
    assert!(db.renew_garmin_job(&first, at(10)?).await?);
    let second = restart
        .claim_garmin_job(at(11)?, today, 60, true)
        .await?
        .ok_or("Second lease missing")?;
    assert_eq!(second.id(), activity);
    assert_eq!(second.kind(), GarminJobKind::Activities);
    assert_eq!(
        restart
            .finish_garmin_job(&second, at(11)?, JobOutcome::Completed)
            .await?,
        RecordedJobStatus::Completed
    );

    // The renewed lease remains valid through second 69 and becomes eligible after second 70.
    let before_expiry = db
        .claim_garmin_job(at(69)?, today, 60, true)
        .await?
        .ok_or("Third lease missing")?;
    assert_eq!(before_expiry.id(), stress);
    assert_eq!(before_expiry.kind(), GarminJobKind::Endpoint);
    assert_eq!(
        db.finish_garmin_job(&before_expiry, at(69)?, JobOutcome::Completed)
            .await?,
        RecordedJobStatus::Completed
    );
    assert!(matches!(
        db.finish_garmin_job(&first, at(71)?, JobOutcome::Completed)
            .await,
        Err(JobStoreError::LeaseLost)
    ));
    let recovered = restart
        .claim_garmin_job(at(71)?, today, 60, true)
        .await?
        .ok_or("Recovery missing")?;
    assert_eq!(recovered.id(), fit);
    assert_eq!(recovered.attempt(), 2);
    assert!(!db.renew_garmin_job(&first, at(71)?).await?);
    assert_eq!(
        restart
            .finish_garmin_job(
                &recovered,
                at(71)?,
                JobOutcome::Failed {
                    reason: FailureReason::ProviderCooldown,
                    retry_at: Some(at(120)?),
                }
            )
            .await?,
        RecordedJobStatus::Deferred
    );
    let pending: (String, i32, OffsetDateTime) =
        sqlx::query_as("SELECT status, attempts, run_at FROM jobs WHERE id = $1")
            .bind(fit)
            .fetch_one(&inspect)
            .await?;
    assert_eq!(pending, ("pending".to_owned(), 1, at(120)?.into()));
    let remaining = db
        .claim_garmin_job(at(72)?, today, 60, true)
        .await?
        .ok_or("Next lease missing")?;
    assert_eq!(remaining.id(), daily);
    db.finish_garmin_job(&remaining, at(72)?, JobOutcome::Completed)
        .await?;
    let history = db
        .claim_garmin_job(at(73)?, today, 60, true)
        .await?
        .ok_or("History missing")?;
    assert_eq!(history.id(), hr);
    db.finish_garmin_job(&history, at(73)?, JobOutcome::Completed)
        .await?;
    assert!(
        db.claim_garmin_job(at(74)?, today, 60, true)
            .await?
            .is_none()
    );
    let ready = db
        .claim_garmin_job(at(120)?, today, 60, true)
        .await?
        .ok_or("Retry missing")?;
    assert_eq!(ready.id(), fit);
    assert_eq!(ready.attempt(), 2);
    Ok(())
}

async fn add(
    db: &PgPool,
    kind: &str,
    dedup: &str,
    offset: i64,
    payload: &str,
) -> Result<Uuid, Box<dyn Error>> {
    let id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO jobs (id, kind, payload, dedup_key, status, run_at, attempts)
                 VALUES ($1, $2, $3::jsonb, $4, 'pending', $5, 0)",
    )
    .bind(id)
    .bind(kind)
    .bind(payload)
    .bind(dedup)
    .bind(OffsetDateTime::from(at(offset)?))
    .execute(db)
    .await?;
    Ok(id)
}

async fn write_gate(
    db: &PgPool,
    status: &str,
    deadline: Option<StoredInstant>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state (key, value) VALUES ('integration:garmin',
                    jsonb_build_object('status', $1::text, 'blocked_until', $2::text))
                 ON CONFLICT (key) DO UPDATE SET value = excluded.value",
    )
    .bind(status)
    .bind(deadline.map(|time| time.to_string()))
    .execute(db)
    .await?;
    Ok(())
}
