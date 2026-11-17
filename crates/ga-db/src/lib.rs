use std::{str::FromStr, time::Duration};

use sqlx::{
    ConnectOptions, PgPool, Postgres, Transaction,
    postgres::{PgConnectOptions, PgPoolOptions},
};

const SCHEMA_REVISION: &str = "c54f8a72d130";
const STORAGE_LIFECYCLE_LOCK: i64 = 72104622;
const OWNER_ENROLLMENT_LOCK: i64 = 72104625;

#[derive(Clone)]
pub struct Database {
    pool: PgPool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReadinessError {
    StorageDisabled,
    MigrationRequired,
    Unavailable,
}

impl From<sqlx::Error> for ReadinessError {
    fn from(_: sqlx::Error) -> Self {
        Self::Unavailable
    }
}

impl Database {
    pub fn connect_lazy(url: &str) -> Result<Self, ReadinessError> {
        let address = url
            .strip_prefix("postgresql+psycopg://")
            .ok_or(ReadinessError::Unavailable)?;
        let options = PgConnectOptions::from_str(&format!("postgresql://{address}"))?
            .disable_statement_logging()
            .application_name("garmin-ai-rust");
        let pool = PgPoolOptions::new()
            .max_connections(2)
            .min_connections(0)
            .acquire_timeout(Duration::from_millis(750))
            .connect_lazy_with(options);
        Ok(Self { pool })
    }

    pub async fn readiness(&self) -> Result<(), ReadinessError> {
        tokio::time::timeout(Duration::from_millis(1500), self.readiness_transaction())
            .await
            .map_err(|_| ReadinessError::Unavailable)?
    }

    async fn readiness_transaction(&self) -> Result<(), ReadinessError> {
        let mut txn = self.pool.begin_with("BEGIN READ ONLY").await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        let readiness = check_readiness(&mut txn).await;
        txn.rollback().await?;
        readiness
    }
}

async fn check_readiness(txn: &mut Transaction<'_, Postgres>) -> Result<(), ReadinessError> {
    for lock in [STORAGE_LIFECYCLE_LOCK, OWNER_ENROLLMENT_LOCK] {
        let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
            .bind(lock)
            .fetch_one(&mut **txn)
            .await?;
        if !acquired {
            return Err(ReadinessError::Unavailable);
        }
    }
    let erased: bool = sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM app_state WHERE key = $1)")
        .bind("maintenance:erased")
        .fetch_one(&mut **txn)
        .await?;
    if erased {
        return Err(ReadinessError::StorageDisabled);
    }
    let revisions: Vec<String> =
        sqlx::query_scalar("SELECT version_num FROM alembic_version LIMIT 2")
            .fetch_all(&mut **txn)
            .await?;
    match revisions.as_slice() {
        [revision] if revision == SCHEMA_REVISION => Ok(()),
        _ => Err(ReadinessError::MigrationRequired),
    }
}
