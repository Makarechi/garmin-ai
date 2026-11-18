//! Recover a logical backup into the pinned schema under the maintenance locks.

use std::{
    collections::BTreeMap,
    error::Error,
    fmt,
    io::{self, BufRead, Read},
};

use serde::Deserialize;
use serde_json::value::RawValue;
use sqlx::{AssertSqlSafe, Postgres, Transaction};

use crate::{
    Database, OWNER_ENROLLMENT_LOCK, SCHEMA_REVISION, STORAGE_LIFECYCLE_LOCK,
    database_export::{ExportCounts, FORMAT, TABLES},
    runtime_lease::RUNTIME_LOCK,
};

const MAX_HEADER_BYTES: usize = 64 * 1024;
const MAX_ROW_BYTES: usize = 32 * 1024 * 1024;
const BOOTSTRAP_STATE: &[&str] = &[
    "maintenance:erased",
    "registry:system:contract_digest",
    "registry:metric:catalog_digest",
    "registry:canonical:validated",
];

#[derive(Debug)]
pub enum DatabaseRestoreError {
    Busy,
    Revision,
    NotEmpty,
    InvalidExport,
    LineTooLong,
    Io(io::Error),
    Database,
}

impl fmt::Display for DatabaseRestoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Busy => "Stop the runtime and other writers before restoring data",
            Self::Revision => "Incompatible export or destination schema",
            Self::NotEmpty => "Restore requires an empty destination database",
            Self::InvalidExport => "The database export is invalid or incomplete",
            Self::LineTooLong => "An export line exceeds the size limit",
            Self::Io(_) => "The export could not be read",
            Self::Database => "The database could not be restored",
        })
    }
}

impl Error for DatabaseRestoreError {}

impl From<sqlx::Error> for DatabaseRestoreError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl From<io::Error> for DatabaseRestoreError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<serde_json::Error> for DatabaseRestoreError {
    fn from(_: serde_json::Error) -> Self {
        Self::InvalidExport
    }
}

#[derive(Deserialize)]
struct Header {
    format: String,
    revision: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Record<'a> {
    table: Option<&'a str>,
    #[serde(borrow)]
    row: Option<&'a RawValue>,
    counts: Option<BTreeMap<String, u64>>,
}

impl Database {
    /// The caller supplies an authenticated export or a trusted local logical snapshot.
    /// No rows become visible if parsing, completeness checks, or the database write fails.
    pub async fn restore_current_database(
        &self,
        source: &mut impl BufRead,
    ) -> Result<ExportCounts, DatabaseRestoreError> {
        let mut line = Vec::new();
        read_line(source, &mut line, MAX_HEADER_BYTES)?;
        let header: Header = serde_json::from_slice(&line)?;
        if header.format != FORMAT || header.revision != SCHEMA_REVISION {
            return Err(DatabaseRestoreError::Revision);
        }
        let mut txn = self.work.begin().await?;
        lock_destination(&mut txn).await?;
        check_empty(&mut txn).await?;
        remove_bootstrap(&mut txn).await?;
        let counts = restore_rows(&mut txn, source, &mut line).await?;
        if counts
            .get("inbound_messages")
            .is_some_and(|&count| count > 0)
        {
            restore_inbound_links(&mut txn).await?;
        }
        validate_restored_events(&mut txn).await?;
        reset_sequences(&mut txn).await?;
        txn.commit().await?;
        Ok(counts)
    }
}

async fn lock_destination(txn: &mut Transaction<'_, Postgres>) -> Result<(), DatabaseRestoreError> {
    sqlx::query("SET LOCAL lock_timeout='1500ms'")
        .execute(&mut **txn)
        .await?;
    for lock in [RUNTIME_LOCK, STORAGE_LIFECYCLE_LOCK, OWNER_ENROLLMENT_LOCK] {
        let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(lock)
            .fetch_one(&mut **txn)
            .await?;
        if !acquired {
            return Err(DatabaseRestoreError::Busy);
        }
    }
    let revisions: Vec<String> =
        sqlx::query_scalar("SELECT version_num FROM alembic_version LIMIT 2")
            .fetch_all(&mut **txn)
            .await?;
    if !matches!(revisions.as_slice(), [revision] if revision == SCHEMA_REVISION) {
        return Err(DatabaseRestoreError::Revision);
    }
    Ok(())
}

async fn check_empty(txn: &mut Transaction<'_, Postgres>) -> Result<(), DatabaseRestoreError> {
    for &(table, _) in TABLES {
        let occupied = match table {
            "app_state" => {
                sqlx::query_scalar(
                    "SELECT EXISTS (SELECT 1 FROM app_state WHERE
                     NOT (key = ANY($1) OR starts_with(key, 'bootstrap:')))",
                )
                .bind(BOOTSTRAP_STATE)
                .fetch_one(&mut **txn)
                .await?
            }
            "people" => {
                sqlx::query_scalar(
                    "SELECT count(*)>1 OR count(*)<>count(*) FILTER (WHERE singleton)
                     FROM people",
                )
                .fetch_one(&mut **txn)
                .await?
            }
            "event_definitions" => {
                sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM event_definitions WHERE namespace<>'system')",
                )
                .fetch_one(&mut **txn)
                .await?
            }
            "metric_definitions" => {
                sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM metric_definitions WHERE namespace<>'system')",
                )
                .fetch_one(&mut **txn)
                .await?
            }
            "event_definition_versions"
            | "metric_definition_versions"
            | "event_metric_mappings" => false,
            "module_configs" => !default_pack_settings(txn).await?,
            "conversations" => {
                sqlx::query_scalar(
                    "SELECT count(*) > (SELECT count(*) FROM people)
                      OR count(*) <> count(*) FILTER (WHERE channel='telegram'
                      AND channel_instance_id='primary' AND external_conversation_id='legacy-owner'
                      AND state='{}'::jsonb AND NOT share_owner_memory) FROM conversations",
                )
                .fetch_one(&mut **txn)
                .await?
            }
            _ => {
                // Table names come from the static ordered schema inventory.
                sqlx::query_scalar(AssertSqlSafe(format!(
                    "SELECT EXISTS (SELECT 1 FROM {table})"
                )))
                .fetch_one(&mut **txn)
                .await?
            }
        };
        if occupied {
            return Err(DatabaseRestoreError::NotEmpty);
        }
    }
    Ok(())
}

async fn default_pack_settings(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<bool, DatabaseRestoreError> {
    #[derive(Deserialize)]
    struct Pack {
        key: String,
    }
    let packs: Vec<Pack> = serde_json::from_str(include_str!("../contracts/scenario-packs.json"))?;
    let keys: Vec<_> = packs.iter().map(|pack| pack.key.as_str()).collect();
    Ok(sqlx::query_scalar(
        "SELECT count(*)=0 OR (count(*)=cardinality($1::text[]) AND count(*)=count(*) FILTER (
         WHERE pack_key=ANY($1) AND revision=1 AND outcome_goal IS NULL AND settings='{}'::jsonb
          AND NOT llm_enabled AND tracking_enabled=(pack_key='general_diary')
          AND collection_enabled=(pack_key='general_diary')
          AND reminders_enabled=(pack_key='general_diary') AND visible=(pack_key='general_diary')))
         FROM module_configs",
    )
    .bind(keys)
    .fetch_one(&mut **txn)
    .await?)
}

async fn remove_bootstrap(txn: &mut Transaction<'_, Postgres>) -> Result<(), DatabaseRestoreError> {
    for statement in [
        "DELETE FROM conversations",
        "DELETE FROM module_configs",
        "DELETE FROM event_metric_mappings",
        "DELETE FROM event_definitions",
        "DELETE FROM metric_definitions",
        "DELETE FROM people",
        "DELETE FROM app_state",
    ] {
        sqlx::query(statement).execute(&mut **txn).await?;
    }
    Ok(())
}

async fn restore_inbound_links(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), DatabaseRestoreError> {
    let result = sqlx::query(
        "UPDATE inbound_messages AS message SET supersedes_id=link.supersedes_id
         FROM pg_temp.garmin_restore_inbound_links AS link WHERE message.id=link.id",
    )
    .execute(&mut **txn)
    .await;
    match result {
        Err(sqlx::Error::Database(error)) if error.is_foreign_key_violation() => {
            Err(DatabaseRestoreError::InvalidExport)
        }
        result => Ok(result.map(|_| ())?),
    }
}

struct InsertPlan {
    columns: Vec<String>,
    float_columns: Vec<String>,
    statement: String,
}

impl InsertPlan {
    async fn load(
        txn: &mut Transaction<'_, Postgres>,
        table: &str,
    ) -> Result<Self, DatabaseRestoreError> {
        let fields: Vec<(String, bool, bool)> = sqlx::query_as(
            "SELECT attname::text, atttypid='jsonb'::regtype,
                    atttypid IN ('float4'::regtype,'float8'::regtype) FROM pg_attribute
             WHERE attrelid=to_regclass($1) AND attnum>0 AND NOT attisdropped ORDER BY attnum",
        )
        .bind(table)
        .fetch_all(&mut **txn)
        .await?;
        if fields.is_empty() {
            return Err(DatabaseRestoreError::Revision);
        }
        let stage_links = table == "inbound_messages";
        if stage_links {
            sqlx::query(
                "CREATE TEMP TABLE garmin_restore_inbound_links
                 (id UUID PRIMARY KEY, supersedes_id UUID NOT NULL) ON COMMIT DROP",
            )
            .execute(&mut **txn)
            .await?;
        }
        let mut columns = Vec::new();
        let mut float_columns = Vec::new();
        let mut projection = Vec::new();
        for (name, is_jsonb, float) in fields {
            if !name
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
            {
                return Err(DatabaseRestoreError::Revision);
            }
            projection.push(if stage_links && name == "supersedes_id" {
                "NULL::uuid".to_owned()
            } else if is_jsonb {
                format!("$1::jsonb->'{name}'")
            } else {
                format!("restored.\"{name}\"")
            });
            if float {
                float_columns.push(name.clone());
            }
            columns.push(name);
        }
        let projection = projection.join(",");
        let statement = if stage_links {
            format!(
                "WITH restored AS (
                     SELECT * FROM json_populate_record(NULL::inbound_messages,$1::json)
                 ), links AS (
                     INSERT INTO pg_temp.garmin_restore_inbound_links
                     SELECT id,supersedes_id FROM restored WHERE supersedes_id IS NOT NULL
                 ) INSERT INTO inbound_messages SELECT {projection} FROM restored"
            )
        } else {
            format!(
                "INSERT INTO {table} SELECT {projection}
                 FROM json_populate_record(NULL::{table},$1::json) AS restored"
            )
        };
        Ok(Self {
            columns,
            float_columns,
            statement,
        })
    }

    async fn insert(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        table: &str,
        row: &RawValue,
        repair: Option<&LegacyFloatTokens>,
    ) -> Result<(), DatabaseRestoreError> {
        let fields: BTreeMap<String, &RawValue> = serde_json::from_str(row.get())?;
        if fields.len() != self.columns.len()
            || !self.columns.iter().all(|field| fields.contains_key(field))
        {
            return Err(DatabaseRestoreError::InvalidExport);
        }
        if let Some(repair) = repair
            && repair.offsets.iter().any(|offset| {
                let rewritten = repair.document[*offset..].as_ptr();
                !self.float_columns.iter().any(|column| {
                    fields
                        .get(column)
                        .is_some_and(|value| std::ptr::eq(value.get().as_ptr(), rewritten))
                })
            })
        {
            return Err(DatabaseRestoreError::InvalidExport);
        }
        if table == "app_state"
            && fields
                .get("key")
                .and_then(|key| serde_json::from_str::<String>(key.get()).ok())
                .as_deref()
                == Some("maintenance:erased")
        {
            return Err(DatabaseRestoreError::InvalidExport);
        }
        sqlx::query(AssertSqlSafe(self.statement.as_str()))
            .bind(row.get())
            .execute(&mut **txn)
            .await?;
        Ok(())
    }
}

async fn restore_rows(
    txn: &mut Transaction<'_, Postgres>,
    source: &mut impl BufRead,
    line: &mut Vec<u8>,
) -> Result<ExportCounts, DatabaseRestoreError> {
    let mut counts: ExportCounts = TABLES.iter().map(|&(table, _)| (table, 0)).collect();
    let mut tables = TABLES.iter().map(|&(table, _)| table);
    let mut current = None;
    let mut plan: Option<InsertPlan> = None;
    loop {
        read_line(source, line, MAX_ROW_BYTES)?;
        let repaired;
        let (record, repair): (Record<'_>, _) = match serde_json::from_slice(line) {
            Ok(record) => (record, None),
            Err(error) => {
                repaired = quote_legacy_float_tokens(line).ok_or(error)?;
                (serde_json::from_slice(&repaired.document)?, Some(&repaired))
            }
        };
        match (record.table, record.row, record.counts) {
            (Some(table), Some(row), None) => {
                if current != Some(table) {
                    let table = tables
                        .find(|candidate| *candidate == table)
                        .ok_or(DatabaseRestoreError::InvalidExport)?;
                    plan = Some(InsertPlan::load(txn, table).await?);
                    current = Some(table);
                }
                plan.as_ref()
                    .ok_or(DatabaseRestoreError::InvalidExport)?
                    .insert(txn, table, row, repair)
                    .await?;
                let count = counts
                    .get_mut(table)
                    .ok_or(DatabaseRestoreError::InvalidExport)?;
                *count = count
                    .checked_add(1)
                    .ok_or(DatabaseRestoreError::InvalidExport)?;
            }
            (None, None, Some(expected)) => {
                if expected.len() != counts.len()
                    || counts
                        .iter()
                        .any(|(&table, count)| expected.get(table) != Some(count))
                {
                    return Err(DatabaseRestoreError::InvalidExport);
                }
                loop {
                    let pending = source.fill_buf()?;
                    if pending.is_empty() {
                        break;
                    }
                    if !pending.iter().all(u8::is_ascii_whitespace) {
                        return Err(DatabaseRestoreError::InvalidExport);
                    }
                    let len = pending.len();
                    source.consume(len);
                }
                return Ok(counts);
            }
            _ => return Err(DatabaseRestoreError::InvalidExport),
        }
    }
}

struct LegacyFloatTokens {
    document: Vec<u8>,
    offsets: Vec<usize>,
}

fn quote_legacy_float_tokens(line: &[u8]) -> Option<LegacyFloatTokens> {
    // Python's writer emits non-finite values as bare tokens. PostgreSQL's
    // floating-point input accepts their string form without changing the value.
    let mut normalized = Vec::new();
    let mut offsets = Vec::new();
    let mut start = 0;
    let mut quoted = false;
    let mut escaped = false;
    for (offset, &byte) in line.iter().enumerate() {
        if offset < start {
            continue;
        }
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
        } else if let Some(token) = [b"-Infinity".as_slice(), b"Infinity", b"NaN"]
            .into_iter()
            .find(|token| line[offset..].starts_with(token))
        {
            let end = offset + token.len();
            if line.get(end).is_some_and(|next| {
                next.is_ascii_whitespace() || matches!(next, b',' | b'}' | b']')
            }) && offset
                .checked_sub(1)
                .and_then(|index| line.get(index))
                .is_some_and(|previous| {
                    previous.is_ascii_whitespace() || matches!(previous, b':' | b',' | b'[')
                })
            {
                normalized.extend_from_slice(&line[start..offset]);
                offsets.push(normalized.len());
                normalized.push(b'"');
                normalized.extend_from_slice(token);
                normalized.push(b'"');
                start = end;
            }
        }
    }
    if start == 0 {
        return None;
    }
    normalized.extend_from_slice(&line[start..]);
    Some(LegacyFloatTokens {
        document: normalized,
        offsets,
    })
}

fn read_line(
    source: &mut impl BufRead,
    line: &mut Vec<u8>,
    limit: usize,
) -> Result<(), DatabaseRestoreError> {
    line.clear();
    let count = source.take((limit + 1) as u64).read_until(b'\n', line)?;
    if count > limit {
        return Err(DatabaseRestoreError::LineTooLong);
    }
    if count == 0 {
        return Err(DatabaseRestoreError::InvalidExport);
    }
    Ok(())
}

async fn validate_restored_events(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), DatabaseRestoreError> {
    let valid: bool = sqlx::query_scalar(
        "SELECT (SELECT count(*)=1 FROM people WHERE singleton)
         AND NOT EXISTS (SELECT 1 FROM events WHERE definition_version_id IS NULL)",
    )
    .fetch_one(&mut **txn)
    .await?;
    if !valid {
        return Err(DatabaseRestoreError::InvalidExport);
    }
    Ok(())
}

async fn reset_sequences(txn: &mut Transaction<'_, Postgres>) -> Result<(), DatabaseRestoreError> {
    for (statement, sequence) in [
        ("SELECT max(id) FROM audit_log", "audit_log_id_seq"),
        (
            "SELECT max(id) FROM telegram_updates",
            "telegram_updates_id_seq",
        ),
    ] {
        let maximum: Option<i64> = sqlx::query_scalar(statement).fetch_one(&mut **txn).await?;
        let start = maximum
            .unwrap_or(0)
            .max(0)
            .checked_add(1)
            .ok_or(DatabaseRestoreError::InvalidExport)?;
        // DDL RESTART is transactional; a rollback must not alter the previous sequence position.
        sqlx::query(AssertSqlSafe(format!(
            "ALTER SEQUENCE {sequence} RESTART WITH {start}"
        )))
        .execute(&mut **txn)
        .await?;
    }
    Ok(())
}
