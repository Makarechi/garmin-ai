use std::{env, error::Error, io, str::FromStr};

use ga_db::{Database, ReadinessError, garmin_owner::OwnerVerificationError};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses a freshly migrated, disposable TimescaleDB"]
async fn real_postgres_owner_verification_is_read_only_and_fails_closed()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url.strip_prefix("postgresql+psycopg://").ok_or_else(|| {
        io::Error::other("GA_TEST_DATABASE_URL must target a PostgreSQL test database")
    })?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, name);

    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&inspect)
        .await?;
    let bindings: i64 = sqlx::query_scalar("SELECT count(*) FROM source_connections")
        .fetch_one(&inspect)
        .await?;
    let legacy: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_state WHERE key = 'account:garmin')")
            .fetch_one(&inspect)
            .await?;
    assert_eq!((bindings, legacy), (0, false));
    let database =
        Database::connect_lazy(&url).map_err(|_| io::Error::other("Invalid fixture URL"))?;
    let good = "a".repeat(64);
    let other = "b".repeat(64);
    assert_eq!(
        database.verify_existing_garmin_owner(&good).await,
        Err(OwnerVerificationError::EnrollmentRequired)
    );
    assert_eq!(
        database
            .verify_existing_garmin_owner("synthetic-invalid-hash")
            .await,
        Err(OwnerVerificationError::InvalidFingerprint)
    );

    sqlx::query("INSERT INTO app_state (key, value) VALUES ('account:garmin', jsonb_build_object('fingerprint', $1::text))")
        .bind(&good).execute(&inspect).await?;
    assert_eq!(
        database
            .verify_existing_garmin_owner(&good)
            .await?
            .owner_id(),
        owner
    );
    let unmodified: i64 = sqlx::query_scalar("SELECT count(*) FROM source_connections")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(unmodified, 0);

    let id = Uuid::new_v4();
    sqlx::query("INSERT INTO source_connections (id, owner_id, provider, namespace, external_id, confirmation_method, details)
                 VALUES ($1, $2, 'garmin', 'socialProfile.profileId:v1', $3, 'synthetic-test', '{}'::jsonb)")
        .bind(id).bind(owner).bind(&other).execute(&inspect).await?;
    for fingerprint in [&good, &other] {
        assert_eq!(
            database.verify_existing_garmin_owner(fingerprint).await,
            Err(OwnerVerificationError::Mismatch)
        );
    }
    sqlx::query("UPDATE source_connections SET external_id = $2 WHERE id = $1")
        .bind(id)
        .bind(&good)
        .execute(&inspect)
        .await?;
    assert_eq!(
        database
            .verify_existing_garmin_owner(&good)
            .await?
            .owner_id(),
        owner
    );
    sqlx::query(
        "UPDATE app_state SET value = '{\"fingerprint\":123}'::jsonb WHERE key = 'account:garmin'",
    )
    .execute(&inspect)
    .await?;
    assert_eq!(
        database.verify_existing_garmin_owner(&good).await,
        Err(OwnerVerificationError::InvalidStoredBinding)
    );

    sqlx::query("DELETE FROM app_state WHERE key = 'account:garmin'")
        .execute(&inspect)
        .await?;
    assert_eq!(
        database
            .verify_existing_garmin_owner(&good)
            .await?
            .owner_id(),
        owner
    );
    let legacy_recreated: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_state WHERE key = 'account:garmin')")
            .fetch_one(&inspect)
            .await?;
    assert!(!legacy_recreated);

    let mut enrollment = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104625)")
        .execute(&mut *enrollment)
        .await?;
    assert_eq!(
        database.verify_existing_garmin_owner(&good).await,
        Err(OwnerVerificationError::Storage(ReadinessError::Unavailable))
    );
    enrollment.rollback().await?;
    sqlx::query("DELETE FROM source_connections WHERE id = $1")
        .bind(id)
        .execute(&inspect)
        .await?;
    Ok(())
}
