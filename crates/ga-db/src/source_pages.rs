mod projection;

use std::{collections::BTreeSet, error::Error, fmt};

use ga_domain::{
    sha256_hex,
    source_contract::{
        SourceCapabilities, SourceContractError, SourceOperation, SourcePage, SourceRecord,
        SourceRequest, SourceTimestamp,
    },
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

use crate::{
    Database, ReadinessError, activity_documents::invalidate_replay_outputs, check_readiness,
};

const WRITER_LOCK: i64 = 72104619;
const CURSOR_LIMIT: usize = 1000;

#[derive(Debug)]
pub enum SourcePageError {
    Configuration,
    Owner,
    InvalidCursor,
    Correction,
    Mapping,
    Busy,
    Cancelled,
    Contract(SourceContractError),
    Storage(ReadinessError),
    Database,
}

impl fmt::Display for SourcePageError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Configuration => "Source instance configuration is invalid",
            Self::Owner => "Source instance owner is unavailable",
            Self::InvalidCursor => "Recorded source cursor is invalid",
            Self::Correction => "Source changed a record without correction capability",
            Self::Mapping => "Source metric mappings could not be applied",
            Self::Busy => "Source publication is busy",
            Self::Cancelled => "Source publication was cancelled",
            Self::Contract(error) => return error.fmt(f),
            Self::Storage(_) => "Source publication storage is unavailable",
            Self::Database => "Source page could not be saved",
        })
    }
}
impl Error for SourcePageError {}
impl From<sqlx::Error> for SourcePageError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}
impl From<SourceContractError> for SourcePageError {
    fn from(error: SourceContractError) -> Self {
        Self::Contract(error)
    }
}

pub enum SourcePoll {
    Ready(Box<PreparedSourcePoll>),
    Disabled,
    Deferred(StoredInstant),
}

pub struct PreparedSourcePoll {
    owner: Uuid,
    instance: String,
    cursor_key: String,
    previous_state: Option<Value>,
    previous_cursors: BTreeSet<String>,
    cursor: Option<String>,
    start: StoredInstant,
    end: StoredInstant,
    saved_start: String,
    saved_end: String,
    now: StoredInstant,
}

impl PreparedSourcePoll {
    pub fn window(&self) -> (StoredInstant, StoredInstant) {
        (self.start, self.end)
    }
    pub fn cursor(&self) -> Option<&str> {
        self.cursor.as_deref()
    }
}

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub enum PublicationState {
    Complete,
    Partial,
    Deferred,
    Disabled,
    Stale,
}

pub struct PublishedSourcePage {
    pub state: PublicationState,
    pub records: usize,
}

impl Database {
    /// Resolve the persisted source window and cursor before contacting its adapter.
    pub async fn prepare_source_poll(
        &self,
        owner: Uuid,
        instance: &str,
        now: StoredInstant,
    ) -> Result<SourcePoll, SourcePageError> {
        if !valid_instance(instance) {
            return Err(SourcePageError::Configuration);
        }
        let key = format!("source-plugin:cursor:{instance}");
        let mut txn = self
            .work
            .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await?;
        check_source_storage(&mut txn, owner).await?;
        if !crate::scenario_packs::source_instance_selected(&mut txn, instance).await? {
            return Ok(SourcePoll::Disabled);
        }
        let previous = read_state(&mut txn, &key).await?;
        txn.commit().await?;
        let saved = previous.as_ref().unwrap_or(&Value::Null);
        if let Some(retry) = read_time(&saved["retry_after"])?
            && retry > now
        {
            return Ok(SourcePoll::Deferred(retry));
        }
        let cursor = match &saved["next_cursor"] {
            Value::String(cursor) if (1..=1000).contains(&cursor.chars().count()) => {
                Some(cursor.clone())
            }
            Value::Null => None,
            _ => return Err(SourcePageError::InvalidCursor),
        };
        let (start, end, saved_start, saved_end, previous_cursors) = if let Some(cursor) =
            cursor.as_ref()
        {
            let start = read_time(&saved["window_start"])?.ok_or(SourcePageError::InvalidCursor)?;
            let end = read_time(&saved["window_end"])?.ok_or(SourcePageError::InvalidCursor)?;
            if end <= start {
                return Err(SourcePageError::InvalidCursor);
            }
            let start_text = saved["window_start"]
                .as_str()
                .ok_or(SourcePageError::InvalidCursor)?;
            let end_text = saved["window_end"]
                .as_str()
                .ok_or(SourcePageError::InvalidCursor)?;
            let mut history = read_cursor_hashes(&saved["seen_cursor_hashes"])?;
            history.insert(sha256_hex(cursor.as_bytes()));
            (
                start,
                end,
                start_text.to_owned(),
                end_text.to_owned(),
                history,
            )
        } else {
            let start: StoredInstant = OffsetDateTime::from(now)
                .checked_sub(Duration::days(7))
                .ok_or(SourcePageError::InvalidCursor)?
                .try_into()
                .map_err(|_| SourcePageError::InvalidCursor)?;
            (
                start,
                now,
                start.to_string(),
                now.to_string(),
                BTreeSet::new(),
            )
        };
        Ok(SourcePoll::Ready(Box::new(PreparedSourcePoll {
            owner,
            instance: instance.to_owned(),
            cursor_key: key,
            previous_state: previous,
            previous_cursors,
            cursor,
            start,
            end,
            saved_start,
            saved_end,
            now,
        })))
    }

    /// Commit a valid page, its source revisions, mapped metrics, and next cursor atomically.
    pub async fn publish_source_page(
        &self,
        poll: PreparedSourcePoll,
        configured_source_hash: &str,
        capabilities: SourceCapabilities,
        page: &SourcePage,
    ) -> Result<PublishedSourcePage, SourcePageError> {
        self.publish_source_page_unless_cancelled(
            poll,
            configured_source_hash,
            capabilities,
            page,
            || false,
        )
        .await
    }

    /// Leave the saved cursor and facts unchanged when a live poll is cancelled before commit.
    pub async fn publish_source_page_unless_cancelled(
        &self,
        poll: PreparedSourcePoll,
        configured_source_hash: &str,
        capabilities: SourceCapabilities,
        page: &SourcePage,
        is_cancelled: impl Fn() -> bool,
    ) -> Result<PublishedSourcePage, SourcePageError> {
        if is_cancelled() {
            return Err(SourcePageError::Cancelled);
        }
        if !valid_sha256(configured_source_hash) {
            return Err(SourcePageError::Configuration);
        }
        let digest = page.check_request(&SourceRequest {
            instance_id: &poll.instance,
            start: poll.start,
            end: poll.end,
            cursor: poll.cursor(),
            previous_cursors: &poll.previous_cursors,
            capabilities,
        })?;
        let mut history = BTreeSet::new();
        if let Some(digest) = digest {
            history = poll.previous_cursors.clone();
            history.insert(digest);
        }
        let snapshot = json!({
            "window_start": poll.saved_start, "window_end": poll.saved_end,
            "next_cursor": page.next_cursor, "seen_cursor_hashes": history,
            "retry_after": page.retry_after.map(SourceTimestamp::isoformat),
            "last_completed_at": page.next_cursor.is_none().then(|| poll.now.to_string()),
        });
        let mut txn = self.work.begin().await?;
        check_source_storage(&mut txn, poll.owner).await?;
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(WRITER_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !locked {
            return Err(SourcePageError::Busy);
        }
        if !crate::scenario_packs::source_instance_selected(&mut txn, &poll.instance).await? {
            return Ok(PublishedSourcePage {
                state: PublicationState::Disabled,
                records: 0,
            });
        }
        if read_state(&mut txn, &poll.cursor_key).await? != poll.previous_state {
            return Ok(PublishedSourcePage {
                state: PublicationState::Stale,
                records: 0,
            });
        }
        let mappings = projection::confirmed_mappings(
            &mut txn,
            poll.owner,
            &poll.instance,
            configured_source_hash,
        )
        .await?;
        let mut changed = false;
        for record in &page.records {
            let raw = publish_raw_record(
                &mut txn,
                &poll.instance,
                record,
                page.fetched_at,
                capabilities.corrections,
            )
            .await?;
            changed |= projection::project_record(
                &mut txn,
                &poll.instance,
                &mappings,
                record,
                &raw,
                page.fetched_at,
                capabilities.time_semantics,
            )
            .await?;
        }
        if changed {
            invalidate_replay_outputs(&mut txn).await?;
        }
        write_state(&mut txn, &poll.cursor_key, &snapshot).await?;
        if is_cancelled() {
            return Err(SourcePageError::Cancelled);
        }
        txn.commit().await?;
        let state = if page.next_cursor.is_none() {
            PublicationState::Complete
        } else if page
            .retry_after
            .is_some_and(|until| until.instant() > poll.now)
        {
            PublicationState::Deferred
        } else {
            PublicationState::Partial
        };
        Ok(PublishedSourcePage {
            state,
            records: page.records.len(),
        })
    }
}

async fn check_source_storage(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<(), SourcePageError> {
    sqlx::query("SET LOCAL statement_timeout = '750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn)
        .await
        .map_err(SourcePageError::Storage)?;
    let found: bool = sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM people WHERE id=$1)")
        .bind(owner)
        .fetch_one(&mut **txn)
        .await?;
    if !found {
        return Err(SourcePageError::Owner);
    }
    Ok(())
}

fn valid_instance(instance: &str) -> bool {
    let mut bytes = instance.bytes();
    instance.len() <= 200
        && bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.:-".contains(&byte)
        })
}

fn valid_sha256(hash: &str) -> bool {
    hash.len() == 64
        && hash
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn read_time(value: &Value) -> Result<Option<StoredInstant>, SourcePageError> {
    match value {
        Value::String(text) => text
            .parse()
            .map(Some)
            .map_err(|_| SourcePageError::InvalidCursor),
        Value::Null => Ok(None),
        _ => Err(SourcePageError::InvalidCursor),
    }
}

fn read_cursor_hashes(value: &Value) -> Result<BTreeSet<String>, SourcePageError> {
    if value.is_null() {
        return Ok(BTreeSet::new());
    }
    value
        .as_array()
        .filter(|items| items.len() <= CURSOR_LIMIT)
        .ok_or(SourcePageError::InvalidCursor)?
        .iter()
        .map(|item| {
            item.as_str()
                .filter(|hash| valid_sha256(hash))
                .map(str::to_owned)
                .ok_or(SourcePageError::InvalidCursor)
        })
        .collect()
}

async fn read_state(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
) -> Result<Option<Value>, SourcePageError> {
    let stored: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(key)
            .fetch_optional(&mut **txn)
            .await?;
    stored
        .map(|value| serde_json::from_str(&value).map_err(|_| SourcePageError::Database))
        .transpose()
}

async fn write_state(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    value: &Value,
) -> Result<(), SourcePageError> {
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
    )
    .bind(key)
    .bind(value.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

struct RawRevision {
    id: Uuid,
    identity: String,
    revision: i64,
}

async fn publish_raw_record(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
    record: &SourceRecord,
    fetched: SourceTimestamp,
    corrections: bool,
) -> Result<RawRevision, SourcePageError> {
    let document = record.canonical_json()?;
    let digest = sha256_hex(&document);
    let document = String::from_utf8(document).map_err(|_| SourcePageError::Database)?;
    let identity = sha256_hex(record.source_record_id.as_bytes());
    let state_key = format!("source-plugin:record:{instance}:{identity}");
    let previous = read_state(txn, &state_key).await?;
    let recorded = previous.as_ref().unwrap_or(&Value::Null);
    let previous_hash = recorded["hash"].as_str();
    if previous.is_some()
        && previous_hash != Some(&digest)
        && record.operation == SourceOperation::Upsert
        && !corrections
    {
        return Err(SourcePageError::Correction);
    }
    let source = format!("plugin:{instance}");
    let candidate = Uuid::new_v4();
    let inserted: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,payload,archive_key,
               fetched_at,source_updated_at,parser_version,status)
         VALUES($1,$2,'source-record-v1',$3,$4,$5::jsonb,$6,$7,$8,1,'raw_only')
         ON CONFLICT (source,endpoint,source_key,payload_hash) DO NOTHING RETURNING id",
    ).bind(candidate).bind(&source).bind(&record.source_record_id).bind(&digest).bind(document)
        .bind(format!("inline:{digest}")).bind(OffsetDateTime::from(fetched.instant()))
        .bind(OffsetDateTime::from(record.observed_at.instant())).fetch_optional(&mut **txn).await?;
    let id = if let Some(id) = inserted {
        id
    } else {
        sqlx::query_scalar(
            "SELECT id FROM source_payloads WHERE source=$1 AND endpoint='source-record-v1'
              AND source_key=$2 AND payload_hash=$3",
        )
        .bind(source)
        .bind(&record.source_record_id)
        .bind(&digest)
        .fetch_one(&mut **txn)
        .await?
    };
    let operation = record.operation.label();
    let old_revision = recorded["revision"].as_i64();
    let revision = if previous_hash != Some(&digest)
        || recorded["operation"] != operation
        || old_revision.is_none()
    {
        let revision = old_revision
            .unwrap_or_default()
            .checked_add(1)
            .ok_or(SourcePageError::Database)?;
        let journal_key = format!("source-plugin:revision:{instance}:{identity}:{revision:08}");
        let journal = json!({
            "hash": digest, "operation": operation,
            "observed_at": record.observed_at.isoformat(), "fetched_at": fetched.isoformat(),
        });
        sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
            .bind(journal_key)
            .bind(journal.to_string())
            .execute(&mut **txn)
            .await?;
        write_state(
            txn,
            &state_key,
            &json!({"hash": digest, "operation": operation, "revision": revision}),
        )
        .await?;
        revision
    } else {
        old_revision.ok_or(SourcePageError::Database)?
    };
    Ok(RawRevision {
        id,
        identity,
        revision,
    })
}
