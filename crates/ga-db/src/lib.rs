pub mod action_tokens;
pub mod activity_documents;
pub mod activity_scans;
pub mod channel_ingress;
pub mod conversation_memory;
pub mod delivery;
pub mod diary;
pub mod fit_ingest;
pub mod fit_staging;
pub mod garmin_endpoint;
mod garmin_freshness;
pub mod garmin_owner;
pub mod garmin_periodic;
pub mod health_analysis;
mod health_projection;
pub mod history_windows;
pub mod jobs;
pub mod model_gate;
pub mod outbox_claims;
pub mod outbox_enqueue;
pub mod proactive_control;
pub mod raw_capture;
pub mod replay;
pub mod runtime_lease;
pub mod scenario_packs;
pub mod share_policy;
pub mod source_pages;
pub mod system_definitions;
pub mod system_metrics;
pub mod telegram_binding;
pub mod telegram_cursor;

use std::{str::FromStr, time::Duration};

use sqlx::{ConnectOptions, Transaction};
use sqlx_postgres::{PgConnectOptions, PgPool, PgPoolOptions, Postgres};

const SCHEMA_REVISION: &str = "c54f8a72d130";
const STORAGE_LIFECYCLE_LOCK: i64 = 72104622;
const OWNER_ENROLLMENT_LOCK: i64 = 72104625;

#[derive(Clone)]
pub struct Database {
    control: PgPool,
    work: PgPool,
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
        Ok(Self {
            control: small_pool(options.clone()),
            work: small_pool(options),
        })
    }

    /// Read physical database size for the backup staging estimate.
    pub async fn backup_database_bytes(&self) -> Result<u64, ReadinessError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            let size: i64 = sqlx::query_scalar("SELECT pg_database_size(current_database())")
                .fetch_one(&mut *txn)
                .await?;
            txn.commit().await?;
            u64::try_from(size).map_err(|_| ReadinessError::Unavailable)
        })
        .await
        .map_err(|_| ReadinessError::Unavailable)?
    }

    pub async fn readiness(&self) -> Result<(), ReadinessError> {
        tokio::time::timeout(Duration::from_millis(1500), self.readiness_transaction())
            .await
            .map_err(|_| ReadinessError::Unavailable)?
    }

    async fn readiness_transaction(&self) -> Result<(), ReadinessError> {
        let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        let readiness = check_readiness(&mut txn).await;
        txn.rollback().await?;
        readiness
    }
}

fn small_pool(options: PgConnectOptions) -> PgPool {
    PgPoolOptions::new()
        .max_connections(2)
        .min_connections(0)
        .acquire_timeout(Duration::from_millis(750))
        .connect_lazy_with(options)
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

pub mod garmin_integration;
