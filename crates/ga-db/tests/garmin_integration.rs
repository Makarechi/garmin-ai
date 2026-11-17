use std::{env, error::Error, io, str::FromStr, time::Duration};

use ga_db::{
    Database,
    garmin_integration::{GarminIntegrationError, GarminTransportOutcome},
    garmin_owner::OwnerVerificationError,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Date, Month, OffsetDateTime};
use uuid::Uuid;

fn at(seconds: i64) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::from_unix_microseconds(
        1_800_000_000_000_000 + seconds * 1_000_000,
    )?)
}

#[tokio::test]
#[ignore = "uses a freshly migrated disposable TimescaleDB"]
async fn stores_garmin_cooldown_and_distinguishes_manual_login() -> Result<(), Box<dyn Error>> {
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
    let account = "a".repeat(64);
    sqlx::query(
        "INSERT INTO app_state (key, value) VALUES ('account:garmin', jsonb_build_object('fingerprint', $1::text))
         ON CONFLICT (key) DO UPDATE SET value=excluded.value",
    )
    .bind(&account)
    .execute(&inspect)
    .await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    assert!(matches!(
        db.record_garmin_transport(
            &"b".repeat(64),
            at(1)?,
            GarminTransportOutcome::AuthorizedLogin
        )
        .await,
        Err(GarminIntegrationError::Owner(
            OwnerVerificationError::Mismatch
        ))
    ));
    let job = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO jobs (id, kind, payload, dedup_key, run_at, status, attempts)
         VALUES ($1, 'garmin_fit', '{\"activity_id\":\"7000000000\"}'::jsonb,
                     'fit:synthetic-cooldown', $2, 'pending', 0)",
    )
    .bind(job)
    .bind(OffsetDateTime::from(at(0)?))
    .execute(&inspect)
    .await?;
    let day = Date::from_calendar_date(2027, Month::January, 15)?;
    let limited = db
        .record_garmin_transport(
            &account,
            at(10)?,
            GarminTransportOutcome::RateLimited {
                retry_after: Some(Duration::from_secs(3)),
            },
        )
        .await?;
    assert_eq!(limited.blocked_until, Some(at(910)?));
    assert_eq!(limited.failure_count, 1);
    assert!(
        Database::connect_lazy(&url)
            .map_err(|_| "Invalid reopened configuration")?
            .claim_garmin_job(at(11)?, day, 30, false)
            .await?
            .is_none()
    );
    let late = db
        .record_garmin_transport(&account, at(9)?, GarminTransportOutcome::Reachable)
        .await?;
    assert_eq!(late.status, "rate_limited");
    assert_eq!(late.blocked_until, limited.blocked_until);

    let failed = db
        .record_garmin_transport(
            &account,
            at(910)?,
            GarminTransportOutcome::ConnectionFailure { retry_after: None },
        )
        .await?;
    assert_eq!(failed.failure_count, 2);
    let deadline = failed
        .blocked_until
        .ok_or("Expected connection retry deadline")?;
    assert!(deadline >= at(1_030)? && deadline < at(1_035)?);
    let expired = db
        .record_garmin_transport(
            &account,
            at(1040)?,
            GarminTransportOutcome::AuthenticationRequired,
        )
        .await?;
    assert_eq!(expired.status, "reauth_required");
    assert_eq!(expired.failure_count, 3);
    assert_eq!(expired.blocked_until, None);
    let inflight = db
        .record_garmin_transport(&account, at(1041)?, GarminTransportOutcome::Reachable)
        .await?;
    assert_eq!(inflight.status, "reauth_required");
    assert!(
        db.claim_garmin_job(at(2000)?, day, 30, false)
            .await?
            .is_none()
    );

    let resumed = db
        .record_garmin_transport(&account, at(2001)?, GarminTransportOutcome::AuthorizedLogin)
        .await?;
    assert_eq!(resumed.status, "active");
    assert_eq!(resumed.failure_count, 0);
    let lease = db
        .claim_garmin_job(at(2002)?, day, 30, false)
        .await?
        .ok_or("Expected suspended job after login")?;
    assert_eq!(lease.id(), job);
    let stored: String =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = 'integration:garmin'")
            .fetch_one(&inspect)
            .await?;
    assert_eq!(
        serde_json::from_str::<Value>(&stored)?,
        json!({
            "status": "active", "blocked_until": null, "last_attempt": at(2001)?.to_string(),
            "reason_class": null, "failure_count": 0,
        })
    );
    Ok(())
}
