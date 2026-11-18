use std::{cell::Cell, env, error::Error, str::FromStr};

use ga_db::{
    Database,
    garmin_owner::{GarminSetupError, LocalGarminSetupMode, OwnerVerificationError},
    storage_maintenance::StorageMaintenanceError,
};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

const OWNER: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

#[tokio::test]
#[ignore = "proves explicit local enrollment and token publication on disposable TimescaleDB"]
async fn local_login_checks_retained_data_and_preserves_the_verified_owner()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
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
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let login = LocalGarminSetupMode::Login {
        confirm_existing_owner: false,
    };
    let enroll = LocalGarminSetupMode::Enroll;
    let runtime = database.acquire_runtime_lease().await?;
    assert!(matches!(
        database.acquire_local_garmin_setup(login).await,
        Err(GarminSetupError::Lock(StorageMaintenanceError::Busy))
    ));
    drop(runtime);
    let published = Cell::new(0);
    let save = || {
        published.set(published.get() + 1);
        Ok(())
    };
    assert!(matches!(
        database
            .acquire_local_garmin_setup(login)
            .await?
            .publish(OWNER, false, || Err(GarminSetupError::TokenPublication))
            .await,
        Err(GarminSetupError::TokenPublication)
    ));
    assert!(matches!(
        database
            .acquire_local_garmin_setup(login)
            .await?
            .publish(OWNER, true, save)
            .await,
        Err(GarminSetupError::ConfirmationRequired)
    ));
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES
           ('synthetic:retained','{}'),('integration:garmin','{\"status\":\"reauth_required\"}')",
    )
    .execute(&pool)
    .await?;
    assert!(matches!(
        database
            .acquire_local_garmin_setup(login)
            .await?
            .publish(OWNER, false, save)
            .await,
        Err(GarminSetupError::ConfirmationRequired)
    ));
    assert_eq!(published.get(), 0);
    assert!(matches!(
        database
            .acquire_local_garmin_setup(enroll)
            .await?
            .publish(OWNER, true, || Err(GarminSetupError::TokenPublication))
            .await,
        Err(GarminSetupError::TokenPublication)
    ));
    let state = || {
        sqlx::query_as::<_, (bool, String)>(
            "SELECT EXISTS(SELECT 1 FROM source_connections WHERE provider='garmin'),
                (SELECT value->>'status' FROM app_state WHERE key='integration:garmin')",
        )
        .fetch_one(&pool)
    };
    assert_eq!(state().await?, (false, "reauth_required".into()));
    database
        .acquire_local_garmin_setup(enroll)
        .await?
        .publish(OWNER, true, save)
        .await?;
    assert_eq!(state().await?, (true, "active".into()));
    let owner = database.verify_existing_garmin_owner(OWNER).await?;
    assert!(matches!(
        database
            .acquire_local_garmin_setup(enroll)
            .await?
            .publish(OTHER, true, save)
            .await,
        Err(GarminSetupError::Owner(OwnerVerificationError::Mismatch))
    ));
    assert_eq!(published.get(), 1);
    sqlx::query("DELETE FROM source_connections WHERE provider='garmin'")
        .execute(&pool)
        .await?;
    database
        .acquire_local_garmin_setup(login)
        .await?
        .publish(OWNER, true, save)
        .await?;
    let method: String = sqlx::query_scalar(
        "SELECT confirmation_method FROM source_connections WHERE provider='garmin'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(method, "legacy_account_binding");
    sqlx::query("DELETE FROM app_state WHERE key='account:garmin'")
        .execute(&pool)
        .await?;
    database
        .acquire_local_garmin_setup(login)
        .await?
        .publish(OWNER, true, save)
        .await?;
    assert_eq!(database.verify_existing_garmin_owner(OWNER).await?, owner);
    assert_eq!(published.get(), 3);
    Ok(())
}
