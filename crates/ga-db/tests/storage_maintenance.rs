use std::{env, error::Error, str::FromStr};

use ga_db::{Database, ReadinessError, storage_maintenance::StorageMaintenanceError};
use sqlx::{ConnectOptions, Connection, PgConnection, postgres::PgConnectOptions};
use uuid::Uuid;

async fn disposable_storage() -> Result<(Database, PgConnection), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Invalid fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected a disposable database")?;
    let mut inspect = PgConnection::connect_with(&options).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&mut inspect)
        .await?;
    assert_eq!(actual, expected);
    Ok((
        Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?,
        inspect,
    ))
}

#[tokio::test]
#[ignore = "requires a populated disposable TimescaleDB fixture"]
async fn erasure_fences_writers_and_preserves_unrelated_tables() -> Result<(), Box<dyn Error>> {
    let (db, mut inspect) = disposable_storage().await?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&mut inspect)
        .await?;
    let configured: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT FROM app_state WHERE key='test:erase-seed')")
            .fetch_one(&mut inspect)
            .await?;
    assert!(configured, "Expected a separately prepared erasure fixture");
    for lock in [72_104_620_i64, 72_104_622, 72_104_625] {
        let mut active = inspect.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock_shared($1)")
            .bind(lock)
            .execute(&mut *active)
            .await?;
        assert!(matches!(
            db.acquire_storage_maintenance().await,
            Err(StorageMaintenanceError::Busy)
        ));
        active.rollback().await?;
    }
    sqlx::query("CREATE TABLE fixture_fk(owner uuid REFERENCES people(id))")
        .execute(&mut inspect)
        .await?;
    sqlx::query("INSERT INTO fixture_fk(owner) VALUES($1)")
        .bind(owner)
        .execute(&mut inspect)
        .await?;
    let mut maintenance = db.acquire_storage_maintenance().await?;
    assert_eq!(db.readiness().await, Err(ReadinessError::Unavailable));
    assert!(db.bound_personal_goals(owner).await.is_err());
    assert!(maintenance.erase_database().await.is_err());
    let unchanged: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT FROM app_state WHERE key='test:erase-seed')
          AND EXISTS(SELECT FROM events) AND EXISTS(SELECT FROM measurements)",
    )
    .fetch_one(&mut inspect)
    .await?;
    assert!(unchanged);
    sqlx::query("DROP TABLE fixture_fk")
        .execute(&mut inspect)
        .await?;
    maintenance.erase_database().await?;
    maintenance.erase_database().await?;
    assert_eq!(db.readiness().await, Err(ReadinessError::Unavailable));
    maintenance.release().await?;
    assert_eq!(db.readiness().await, Err(ReadinessError::StorageDisabled));
    assert!(db.bound_personal_goals(owner).await.is_err());
    println!(
        "Erasure blocked late writers and kept the database fence after releasing the runtime lock"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires the same disposable fixture after the paired erasure check"]
async fn resume_removes_only_the_database_fence() -> Result<(), Box<dyn Error>> {
    let (db, mut inspect) = disposable_storage().await?;
    assert_eq!(db.readiness().await, Err(ReadinessError::StorageDisabled));
    let mut maintenance = db.acquire_storage_maintenance().await?;
    maintenance.resume_database().await?;
    maintenance.resume_database().await?;
    assert_eq!(db.readiness().await, Err(ReadinessError::Unavailable));
    maintenance.release().await?;
    assert_eq!(db.readiness().await, Ok(()));
    let safe: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS(SELECT FROM people) AND NOT EXISTS(SELECT FROM app_state)
          AND EXISTS(SELECT FROM fixture_unrelated WHERE id=17)",
    )
    .fetch_one(&mut inspect)
    .await?;
    assert!(safe);
    Ok(())
}
