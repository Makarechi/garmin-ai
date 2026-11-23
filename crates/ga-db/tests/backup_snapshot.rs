use std::{env, error::Error, str::FromStr};

use ga_db::{Database, database_export::DatabaseExportError};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, Connection, PgConnection, postgres::PgConnectOptions};

const RAW: &str = "aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json";
const FIT: &str = "bb/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.fit";

#[tokio::test]
#[ignore = "uses an explicitly prepared disposable PostgreSQL backup fixture"]
async fn captures_raw_inventory_with_runtime_and_token_coordination() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Unsupported test URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Only a disposable test database may be used")?;
    let mut inspect = PgConnection::connect_with(&options).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&mut inspect)
        .await?;
    assert_eq!(actual, name);
    let marker: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='test:backup-source-locks')",
    )
    .fetch_one(&mut inspect)
    .await?;
    assert!(
        marker,
        "This test needs an isolated prepared bootstrap fixture"
    );
    sqlx::query(
        "INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,parser_version,status)
         SELECT gen_random_uuid(),'garmin_connect','test',key,repeat('a',64),$1,0,'pending'
         FROM (VALUES('test-one'),('test-two')) AS sources(key)",
    ).bind(RAW).execute(&mut inspect).await?;
    sqlx::query(
        "INSERT INTO activities(id,kind,start,\"end\",timezone,fit_key,details)
         VALUES ('synthetic-activity','running',now(),now(),'UTC',$1,'{}')",
    )
    .bind(FIT)
    .execute(&mut inspect)
    .await?;

    let database =
        Database::connect_lazy(&url).map_err(|_| "Test connection configuration failed")?;
    for lock in [72_104_620_i64, 72_104_626] {
        let mut active = inspect.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock($1)")
            .bind(lock)
            .execute(&mut *active)
            .await?;
        assert!(matches!(
            database.acquire_backup_sources().await,
            Err(DatabaseExportError::Busy)
        ));
        active.rollback().await?;
    }

    let mut lease = database.acquire_backup_sources().await?;
    for lock in [72_104_620_i64, 72_104_626] {
        let available: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(lock)
            .fetch_one(&mut inspect)
            .await?;
        assert!(!available);
    }
    let time = StoredInstant::from_unix_microseconds(1_800_000_000_000_123)?;
    let mut bytes = Vec::new();
    let snapshot = lease.export(time, &mut bytes).await?;
    assert_eq!(snapshot.counts["source_payloads"], 2);
    assert_eq!(snapshot.counts["activities"], 1);
    let mut keys: Vec<_> = snapshot
        .required_raw
        .iter()
        .map(ToString::to_string)
        .collect();
    keys.sort();
    assert_eq!(keys, [RAW, FIT]);
    let rows = String::from_utf8(bytes)?;
    assert!(rows.contains(RAW) && rows.contains(FIT));
    lease.check().await?;

    sqlx::query("UPDATE source_payloads SET archive_key = 'invalid' WHERE source_key = 'test-one'")
        .execute(&mut inspect)
        .await?;
    let mut bytes = Vec::new();
    assert!(matches!(
        lease.export(time, &mut bytes).await,
        Err(DatabaseExportError::ArchiveReference),
    ));
    assert_eq!(bytes, [] as [u8; 0]);
    drop(lease);
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(1);
    loop {
        match database.acquire_backup_sources().await {
            Ok(_) => break,
            Err(DatabaseExportError::Busy) if tokio::time::Instant::now() < deadline => {
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
            }
            Err(error) => return Err(error.into()),
        }
    }
    println!(
        "Captured two distinct raw keys, rejected a malformed key and shared both runtime locks"
    );
    Ok(())
}
