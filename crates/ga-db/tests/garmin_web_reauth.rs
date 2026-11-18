use std::{cell::Cell, env, error::Error, str::FromStr};

use ga_db::{
    Database,
    garmin_integration::{
        GarminIntegrationError, GarminReauthError, GarminReauthStatus, GarminReauthTarget,
    },
    garmin_owner::OwnerVerificationError,
    telegram_binding::TelegramBindingError,
};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "requires a disposable database initialized from the pinned Python schema"]
async fn publishes_only_for_the_verified_owner_and_releases_a_completed_pause()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let target = GarminReauthTarget {
        telegram_instance: "primary",
        telegram_owner: 42,
        garmin_instance: "source:garmin:primary",
    };
    let fingerprint = "a".repeat(64);
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    assert!(matches!(
        database.garmin_web_reauth_status(target).await,
        Err(GarminReauthError::Integration(
            GarminIntegrationError::Owner(OwnerVerificationError::EnrollmentRequired)
        ))
    ));
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES
         ('account:garmin',jsonb_build_object('fingerprint',$1::text)),
         ('integration:garmin','{\"status\":\"reauth_required\",\"reason_class\":\"AuthenticationRequired\",\"failure_count\":1}')",
    ).bind(&fingerprint).execute(&pool).await?;
    assert_eq!(
        database.garmin_web_reauth_status(target).await?,
        GarminReauthStatus::Required
    );

    let calls = Cell::new(0);
    let publish = || {
        calls.set(calls.get() + 1);
        true
    };
    assert!(matches!(
        database
            .commit_garmin_web_reauth(target, &"b".repeat(64), publish)
            .await,
        Err(GarminReauthError::Integration(
            GarminIntegrationError::Owner(OwnerVerificationError::Mismatch)
        ))
    ));
    assert_eq!(
        database.garmin_web_reauth_status(target).await?,
        GarminReauthStatus::Unavailable
    );
    sqlx::query(
        "UPDATE app_state SET value=value || '{\"reason_class\":\"AuthenticationRequired\"}'::jsonb
         WHERE key='integration:garmin'",
    )
    .execute(&pool)
    .await?;
    let mut backup = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104626)")
        .execute(&mut *backup)
        .await?;
    assert!(matches!(
        database
            .commit_garmin_web_reauth(target, &fingerprint, publish)
            .await,
        Err(GarminReauthError::Integration(GarminIntegrationError::Busy))
    ));
    backup.rollback().await?;
    assert_eq!(calls.get(), 0);

    sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE owner_id=$1")
        .bind(owner)
        .execute(&pool)
        .await?;
    assert!(matches!(
        database
            .commit_garmin_web_reauth(target, &fingerprint, publish)
            .await,
        Err(GarminReauthError::Pairing(TelegramBindingError::Mismatch))
    ));
    sqlx::query("UPDATE channel_bindings SET external_id='42' WHERE owner_id=$1")
        .bind(owner)
        .execute(&pool)
        .await?;
    assert_eq!(calls.get(), 0);

    assert!(matches!(
        database
            .commit_garmin_web_reauth(target, &fingerprint, || false)
            .await,
        Err(GarminReauthError::Publication)
    ));
    assert_eq!(
        database.garmin_web_reauth_status(target).await?,
        GarminReauthStatus::Required
    );
    database
        .commit_garmin_web_reauth(target, &fingerprint, publish)
        .await?;
    assert_eq!(
        database.garmin_web_reauth_status(target).await?,
        GarminReauthStatus::Active
    );
    assert!(matches!(
        database
            .commit_garmin_web_reauth(target, &fingerprint, publish)
            .await,
        Err(GarminReauthError::Unavailable)
    ));
    assert_eq!(calls.get(), 1);
    let failures: i32 = sqlx::query_scalar(
        "SELECT (value->>'failure_count')::int FROM app_state WHERE key='integration:garmin'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(failures, 0);
    Ok(())
}
