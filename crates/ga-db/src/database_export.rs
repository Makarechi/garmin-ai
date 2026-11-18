//! Stream the current logical schema from a single committed PostgreSQL snapshot.

use std::{
    collections::BTreeMap,
    error::Error,
    fmt,
    io::{self, Write},
    time::Duration,
};

use futures_util::TryStreamExt;
use ga_archive::ArchiveKey;
use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{Connection, PgConnection, Postgres, Transaction};
use uuid::Uuid;

use crate::{
    Database, OWNER_ENROLLMENT_LOCK, ReadinessError, SCHEMA_REVISION, STORAGE_LIFECYCLE_LOCK,
    check_readiness,
    runtime_lease::{RuntimeLease, RuntimeLeaseError},
};

pub(crate) const FORMAT: &str = "garmin-ai-jsonl-v1";

// Dependency order used by the current Python restore. The differential fixture
// compares this inventory against the pinned SQLAlchemy metadata.
macro_rules! export_tables {
    ($($table:literal),+ $(,)?) => {
        &[$(($table, concat!("SELECT row_to_json(exported)::text FROM \"", $table, "\" AS exported"))),+]
    };
}

pub(crate) const TABLES: &[(&str, &str)] = export_tables![
    "activities",
    "app_state",
    "audit_log",
    "health_days",
    "insights",
    "jobs",
    "pending_questions",
    "people",
    "source_payloads",
    "telegram_updates",
    "timeline_intervals",
    "activity_parts",
    "channel_bindings",
    "conversations",
    "event_definitions",
    "metric_definitions",
    "module_configs",
    "source_connections",
    "event_definition_versions",
    "inbound_messages",
    "metric_definition_versions",
    "tracker_configs",
    "event_metric_mappings",
    "events",
    "measurement_history",
    "measurement_revisions",
    "measurements",
    "outbox_messages",
    "message_delivery_receipts",
    "metric_observations",
];

const TOKEN_PUBLICATION_LOCK: i64 = 72_104_626;

pub struct BackupSnapshot {
    pub counts: ExportCounts,
    pub required_raw: Vec<ArchiveKey>,
}

/// Share the original runtime and token-publication locks with Python for the full backup.
pub struct BackupSourceLease<'a> {
    database: &'a Database,
    runtime: RuntimeLease,
}

pub type ExportCounts = BTreeMap<&'static str, u64>;

#[derive(Debug)]
pub enum DatabaseExportError {
    Storage(ReadinessError),
    Busy,
    Owner,
    ArchiveReference,
    Io(io::Error),
    Database,
}

impl fmt::Display for DatabaseExportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(ReadinessError::StorageDisabled) => "Cannot export erased storage",
            Self::Storage(ReadinessError::MigrationRequired) => "Unexpected database schema",
            Self::Storage(ReadinessError::Unavailable) | Self::Busy => {
                "Backup database storage is busy"
            }
            Self::Owner => "The export requires the enrolled owner",
            Self::ArchiveReference => "The snapshot contains an invalid raw archive reference",
            Self::Io(_) => "Database export could not be written",
            Self::Database => "The database snapshot could not be read",
        })
    }
}
impl Error for DatabaseExportError {}
impl From<io::Error> for DatabaseExportError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}
impl From<sqlx::Error> for DatabaseExportError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Use a dedicated runtime for the writer, which stages output and publishes
    /// only after this call returns the verified table counts.
    pub async fn export_database_rows(
        &self,
        owner: Uuid,
        exported_at: StoredInstant,
        output: &mut impl Write,
    ) -> Result<ExportCounts, DatabaseExportError> {
        Ok(self
            .export_snapshot(owner, exported_at, output, false)
            .await?
            .counts)
    }

    pub async fn acquire_backup_sources(
        &self,
    ) -> Result<BackupSourceLease<'_>, DatabaseExportError> {
        let mut runtime = self
            .acquire_runtime_lease()
            .await
            .map_err(|error| match error {
                RuntimeLeaseError::Occupied => DatabaseExportError::Busy,
                RuntimeLeaseError::Readiness(error) => DatabaseExportError::Storage(error),
                RuntimeLeaseError::Database => DatabaseExportError::Database,
            })?;
        let locked: bool = tokio::time::timeout(
            Duration::from_millis(1500),
            sqlx::query_scalar("SELECT pg_try_advisory_lock($1)")
                .bind(TOKEN_PUBLICATION_LOCK)
                .fetch_one(&mut runtime.connection),
        )
        .await
        .map_err(|_| DatabaseExportError::Database)??;
        if !locked {
            return Err(DatabaseExportError::Busy);
        }
        Ok(BackupSourceLease {
            database: self,
            runtime,
        })
    }

    async fn export_snapshot(
        &self,
        owner: Uuid,
        exported_at: StoredInstant,
        output: &mut impl Write,
        include_archive: bool,
    ) -> Result<BackupSnapshot, DatabaseExportError> {
        let mut connection = self.lock_export_snapshot().await?;
        let mut snapshot = connection
            .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await?;
        sqlx::query("SET LOCAL TIME ZONE 'UTC'")
            .execute(&mut *snapshot)
            .await?;
        check_readiness(&mut snapshot)
            .await
            .map_err(DatabaseExportError::Storage)?;
        let selected: Option<Uuid> = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
            .fetch_optional(&mut *snapshot)
            .await?;
        if selected != Some(owner) {
            return Err(DatabaseExportError::Owner);
        }
        let required_raw = if include_archive {
            required_archive_keys(&mut snapshot).await?
        } else {
            Vec::new()
        };
        let counts = export_rows(&mut snapshot, exported_at, output).await?;
        snapshot.commit().await?;
        // This dedicated session owns the lifecycle lock until the snapshot ends.
        // Closing it releases the lock on success, error and cancellation.
        drop(connection);
        Ok(BackupSnapshot {
            counts,
            required_raw,
        })
    }

    async fn lock_export_snapshot(&self) -> Result<PgConnection, DatabaseExportError> {
        let options = self.control.connect_options();
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut connection = PgConnection::connect_with(options.as_ref()).await?;
            for lock in [STORAGE_LIFECYCLE_LOCK, OWNER_ENROLLMENT_LOCK] {
                let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock_shared($1)")
                    .bind(lock)
                    .fetch_one(&mut connection)
                    .await?;
                if !locked {
                    return Err(DatabaseExportError::Busy);
                }
            }
            Ok(connection)
        })
        .await
        .map_err(|_| DatabaseExportError::Database)?
    }
}

impl BackupSourceLease<'_> {
    /// Inventory and rows come from the same committed source state.
    pub async fn export(
        &mut self,
        exported_at: StoredInstant,
        output: &mut impl Write,
    ) -> Result<BackupSnapshot, DatabaseExportError> {
        let owner = tokio::time::timeout(
            Duration::from_millis(1500),
            sqlx::query_scalar("SELECT id FROM people WHERE singleton")
                .fetch_optional(&mut self.runtime.connection),
        )
        .await
        .map_err(|_| DatabaseExportError::Database)??
        .ok_or(DatabaseExportError::Owner)?;
        self.database
            .export_snapshot(owner, exported_at, output, true)
            .await
    }

    pub async fn check(&mut self) -> Result<(), DatabaseExportError> {
        self.runtime
            .check()
            .await
            .map_err(|_| DatabaseExportError::Database)
    }
}

async fn required_archive_keys(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<Vec<ArchiveKey>, DatabaseExportError> {
    let mut references = sqlx::query_scalar::<_, String>(
        "SELECT archive_key FROM source_payloads
         UNION SELECT fit_key FROM activities WHERE fit_key IS NOT NULL",
    )
    .fetch(&mut **txn);
    let mut keys = Vec::new();
    while let Some(reference) = references.try_next().await? {
        keys.push(
            reference
                .parse()
                .map_err(|_| DatabaseExportError::ArchiveReference)?,
        );
    }
    Ok(keys)
}

async fn export_rows(
    snapshot: &mut Transaction<'_, Postgres>,
    at: StoredInstant,
    output: &mut impl Write,
) -> Result<ExportCounts, DatabaseExportError> {
    writeln!(
        output,
        "{}",
        json!({"format":FORMAT,"revision":SCHEMA_REVISION,"exported_at":at.to_string()})
    )?;
    let mut counts = ExportCounts::new();
    for &(table, query) in TABLES {
        let mut rows = sqlx::query_scalar::<_, String>(query).fetch(&mut **snapshot);
        let mut count = 0_u64;
        while let Some(row) = rows.try_next().await? {
            write!(output, r#"{{"table": "{table}", "row": "#)?;
            write_row_json(output, row.as_bytes())?;
            output.write_all(b"}\n")?;
            count = count.checked_add(1).ok_or(DatabaseExportError::Database)?;
        }
        counts.insert(table, count);
    }
    writeln!(output, "{}", json!({"counts":counts}))?;
    output.flush()?;
    Ok(counts)
}

fn write_row_json(output: &mut impl Write, row: &[u8]) -> io::Result<()> {
    // PostgreSQL writes floating-point negative zero as -0. The legacy reader
    // parses that spelling as an integer, losing the sign before restore.
    let mut start = 0;
    let mut quoted = false;
    let mut escaped = false;
    for (offset, &byte) in row.iter().enumerate() {
        if quoted {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                quoted = false;
            }
        } else if byte == b'"' {
            quoted = true;
        } else if byte == b'-'
            && matches!(
                row.get(offset + 1..offset + 3),
                Some([b'0', b',' | b'}' | b']'])
            )
        {
            let end = offset + 2;
            output.write_all(&row[start..end])?;
            output.write_all(b".0")?;
            start = end;
        }
    }
    output.write_all(&row[start..])
}
