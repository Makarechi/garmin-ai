use std::{
    env,
    error::Error,
    fs::OpenOptions,
    io::{self, Write},
    str::FromStr,
    sync::mpsc,
    thread,
    time::Duration,
};

use ga_db::{Database, ReadinessError, database_export::DatabaseExportError};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, Connection, PgConnection, postgres::PgConnectOptions};
use uuid::Uuid;

struct ChangeAfterHeader<W> {
    destination: W,
    ready: Option<mpsc::Sender<()>>,
    changed: mpsc::Receiver<bool>,
}

impl<W: Write> Write for ChangeAfterHeader<W> {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        self.destination.write_all(data)?;
        if data.contains(&b'\n')
            && let Some(ready) = self.ready.take()
            && (ready.send(()).is_err()
                || self.changed.recv_timeout(Duration::from_secs(4)) != Ok(true))
        {
            return Err(io::Error::other("The concurrent fixture commit failed"));
        }
        Ok(data.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.destination.flush()
    }
}

struct FullDisk;

impl Write for FullDisk {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::ErrorKind::StorageFull.into())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[tokio::test]
#[ignore = "requires the pinned synthetic PostgreSQL fixture and its output path"]
async fn streams_a_current_snapshot_without_activating_or_mutating_storage()
-> Result<(), Box<dyn Error>> {
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
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&mut inspect)
        .await?;
    assert_eq!(current, name);
    let fixture: bool = sqlx::query_scalar(
        "SELECT value = '{\"fixture\":true}'::jsonb FROM app_state WHERE key='test:backup-export'",
    )
    .fetch_one(&mut inspect)
    .await?;
    assert!(fixture);
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&mut inspect)
        .await?;
    let database =
        Database::connect_lazy(&url).map_err(|_| "Test connection configuration failed")?;
    let exported_at = StoredInstant::from_unix_microseconds(1_800_000_000_000_123)?;
    let mut nothing = Vec::new();
    assert!(matches!(
        database
            .export_database_rows(Uuid::nil(), exported_at, &mut nothing)
            .await,
        Err(DatabaseExportError::Owner)
    ));
    assert!(nothing.is_empty());

    for lock in [72104622_i64, 72104625_i64] {
        let mut lifecycle = inspect.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock($1)")
            .bind(lock)
            .execute(&mut *lifecycle)
            .await?;
        assert!(matches!(
            database
                .export_database_rows(owner, exported_at, &mut nothing)
                .await,
            Err(DatabaseExportError::Busy)
        ));
        lifecycle.rollback().await?;
    }
    assert!(nothing.is_empty());

    assert!(matches!(
        database.export_database_rows(owner, exported_at, &mut FullDisk).await,
        Err(DatabaseExportError::Io(error)) if error.kind() == io::ErrorKind::StorageFull
    ));
    // Acquiring the exclusive lock now also checks that failure closed the session.
    let released: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(72104622)")
        .fetch_one(&mut inspect)
        .await?;
    assert!(released);

    let destination = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(env::var("GA_TEST_DATABASE_EXPORT_OUTPUT")?)?;
    let (ready, wait) = mpsc::channel();
    let (changed, result) = mpsc::channel();
    let change = thread::spawn(move || {
        let outcome = wait.recv_timeout(Duration::from_secs(4)).is_ok()
            && tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .is_ok_and(|runtime| runtime.block_on(commit_change(options)));
        let _ = changed.send(outcome);
    });
    let mut writer = ChangeAfterHeader {
        destination,
        ready: Some(ready),
        changed: result,
    };
    let counts = database
        .export_database_rows(owner, exported_at, &mut writer)
        .await?;
    assert!(change.join().is_ok());
    assert_eq!(counts.len(), 30);
    assert_eq!(counts["events"], 1);
    assert_eq!(counts["audit_log"], 1);

    let latest: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM app_state WHERE key='test:committed-after-header')",
    )
    .fetch_one(&mut inspect)
    .await?;
    assert!(latest);
    sqlx::query("INSERT INTO app_state(key,value) VALUES ('maintenance:erased','{}')")
        .execute(&mut inspect)
        .await?;
    assert!(matches!(
        database
            .export_database_rows(owner, exported_at, &mut nothing)
            .await,
        Err(DatabaseExportError::Storage(
            ReadinessError::StorageDisabled
        ))
    ));
    assert!(nothing.is_empty());
    println!("Validated owner and lifecycle gates, writer failure, and a concurrent commit");
    Ok(())
}

async fn commit_change(options: PgConnectOptions) -> bool {
    let Ok(mut connection) = PgConnection::connect_with(&options).await else {
        return false;
    };
    sqlx::query("INSERT INTO app_state(key,value) VALUES ('test:committed-after-header','{}')")
        .execute(&mut connection)
        .await
        .is_ok()
}
