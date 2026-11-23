pub mod action_tokens;
pub mod activity_documents;
pub mod activity_scans;
pub mod channel_ingress;
pub mod conversation_memory;
pub mod database_export;
pub mod database_restore;
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
pub mod observability;
pub mod onboarding;
pub mod outbox_claims;
pub mod outbox_enqueue;
pub mod personal_goals;
pub mod proactive_control;
pub mod raw_capture;
pub mod replay;
pub mod resource_catalog;
pub mod retention;
pub mod runtime_lease;
pub mod scenario_packs;
pub mod schema_migration;
pub mod share_policy;
pub mod source_pages;
pub mod storage_maintenance;
pub mod system_definitions;
pub mod system_metrics;
pub mod telegram_binding;
pub mod telegram_cursor;
mod telegram_garmin;
pub mod web_chat;

use std::{str::FromStr, time::Duration};

use sqlx::{ConnectOptions, Connection, Transaction};
use sqlx_postgres::{PgConnectOptions, PgPool, PgPoolOptions, Postgres};

const SCHEMA_REVISION: &str = include_str!("../schema-revision.txt").trim_ascii();
const STORAGE_LIFECYCLE_LOCK: i64 = 72104622;
const OWNER_ENROLLMENT_LOCK: i64 = 72104625;
const DATABASE_CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const CONTROL_TRANSACTION_TIMEOUT: Duration = Duration::from_millis(1500);

fn query_canceled(error: &sqlx::Error) -> bool {
    error
        .as_database_error()
        .is_some_and(|error| error.code().as_deref() == Some("57014"))
}

#[derive(Clone)]
pub struct Database {
    control: PgPool,
    work: PgPool,
    model_operations: PgPool,
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
        Self::connect(url, false)
    }

    /// Keep all database sessions read-only while inspecting an installation.
    pub fn connect_lazy_read_only(url: &str) -> Result<Self, ReadinessError> {
        Self::connect(url, true)
    }

    fn connect(url: &str, read_only: bool) -> Result<Self, ReadinessError> {
        let address = url
            .strip_prefix("postgresql+psycopg://")
            .ok_or(ReadinessError::Unavailable)?;
        let options = PgConnectOptions::from_str(&format!("postgresql://{address}"))?
            .disable_statement_logging()
            .application_name("garmin-ai-rust");
        let options = if read_only {
            options.options([("default_transaction_read_only", "on")])
        } else {
            options
        };
        Ok(Self {
            control: small_pool(options.clone()),
            work: small_pool(options.clone()),
            model_operations: small_pool(options),
        })
    }

    /// Read physical database size for the backup staging estimate.
    pub async fn backup_database_bytes(&self) -> Result<u64, ReadinessError> {
        let mut connection = self.control.acquire().await?;
        tokio::time::timeout(CONTROL_TRANSACTION_TIMEOUT, async {
            let mut txn = connection.begin_with("BEGIN READ ONLY").await?;
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
        let mut connection = self.control.acquire().await?;
        tokio::time::timeout(CONTROL_TRANSACTION_TIMEOUT, async {
            let mut txn = connection.begin_with("BEGIN READ ONLY").await?;
            sqlx::query("SET LOCAL statement_timeout = '750ms'")
                .execute(&mut *txn)
                .await?;
            let readiness = check_readiness(&mut txn).await;
            txn.rollback().await?;
            readiness
        })
        .await
        .map_err(|_| ReadinessError::Unavailable)?
    }
}

fn small_pool(options: PgConnectOptions) -> PgPool {
    PgPoolOptions::new()
        .max_connections(2)
        .min_connections(0)
        .acquire_timeout(DATABASE_CONNECT_TIMEOUT)
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

pub mod calendar_context;
pub mod garmin_integration;
