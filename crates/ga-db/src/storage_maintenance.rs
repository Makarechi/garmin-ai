//! The database half of an explicit erase or resume operation.

use std::{error::Error, fmt, time::Duration};

use sqlx::{AssertSqlSafe, Connection, PgConnection};

use crate::{
    Database, OWNER_ENROLLMENT_LOCK, SCHEMA_REVISION, STORAGE_LIFECYCLE_LOCK,
    database_export::TABLES, runtime_lease::RUNTIME_LOCK,
};

#[derive(Debug)]
pub enum StorageMaintenanceError {
    Busy,
    Revision,
    Database,
}

impl fmt::Display for StorageMaintenanceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Busy => "Stop the runtime and other writers before changing storage",
            Self::Revision => "Storage maintenance requires the current schema",
            Self::Database => "The storage change could not be committed",
        })
    }
}

impl Error for StorageMaintenanceError {}

impl From<sqlx::Error> for StorageMaintenanceError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

/// Keep the runtime and other writers stopped until the caller finishes file changes.
pub struct StorageMaintenance {
    connection: PgConnection,
}

impl Database {
    /// Acquire maintenance ownership without requiring an active database.
    /// The caller already holds the shared filesystem protocol's exclusive lock.
    pub async fn acquire_storage_maintenance(
        &self,
    ) -> Result<StorageMaintenance, StorageMaintenanceError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut connection = self.locked_maintenance_connection().await?;
            let has_ledger: bool =
                sqlx::query_scalar("SELECT to_regclass('public.alembic_version') IS NOT NULL")
                    .fetch_one(&mut connection)
                    .await?;
            if !has_ledger {
                return Err(StorageMaintenanceError::Revision);
            }
            let revisions: Vec<String> =
                sqlx::query_scalar("SELECT version_num FROM public.alembic_version LIMIT 2")
                    .fetch_all(&mut connection)
                    .await?;
            if !matches!(revisions.as_slice(), [revision] if revision == SCHEMA_REVISION) {
                return Err(StorageMaintenanceError::Revision);
            }
            Ok(StorageMaintenance { connection })
        })
        .await
        .map_err(|_| StorageMaintenanceError::Database)?
    }

    pub(crate) async fn locked_maintenance_connection(
        &self,
    ) -> Result<PgConnection, StorageMaintenanceError> {
        let mut connection =
            PgConnection::connect_with(self.control.connect_options().as_ref()).await?;
        sqlx::query("SET search_path=pg_catalog,public")
            .execute(&mut connection)
            .await?;
        for lock in [RUNTIME_LOCK, STORAGE_LIFECYCLE_LOCK, OWNER_ENROLLMENT_LOCK] {
            let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock($1)")
                .bind(lock)
                .fetch_one(&mut connection)
                .await?;
            if !acquired {
                return Err(StorageMaintenanceError::Busy);
            }
        }
        Ok(connection)
    }
}

impl StorageMaintenance {
    /// After the file erasure marker is durable, clear the supported tables and
    /// their sequences. A retry after a committed erasure keeps the store fenced.
    pub async fn erase_database(&mut self) -> Result<(), StorageMaintenanceError> {
        let mut txn = self.connection.begin().await?;
        sqlx::query("SET LOCAL lock_timeout='1500ms'")
            .execute(&mut *txn)
            .await?;
        let tables = TABLES
            .iter()
            .map(|(name, _)| format!("public.\"{name}\""))
            .collect::<Vec<_>>()
            .join(",");
        sqlx::query(AssertSqlSafe(format!("TRUNCATE {tables} RESTART IDENTITY")))
            .execute(&mut *txn)
            .await?;
        sqlx::query("INSERT INTO public.app_state(key,value) VALUES ('maintenance:erased','{\"disabled\":true}')")
            .execute(&mut *txn).await?;
        txn.commit().await?;
        Ok(())
    }

    /// Clear the database fence while all writers are still stopped. The caller
    /// reactivates the file store before releasing this lease.
    pub async fn resume_database(&mut self) -> Result<(), StorageMaintenanceError> {
        let mut txn = self.connection.begin().await?;
        sqlx::query("SET LOCAL lock_timeout='1500ms'")
            .execute(&mut *txn)
            .await?;
        sqlx::query("DELETE FROM public.app_state WHERE key='maintenance:erased'")
            .execute(&mut *txn)
            .await?;
        txn.commit().await?;
        Ok(())
    }

    pub async fn release(self) -> Result<(), StorageMaintenanceError> {
        self.connection.close().await?;
        Ok(())
    }
}
