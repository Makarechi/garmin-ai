use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::json;
use sqlx::{Connection, Postgres, Transaction};
use time::OffsetDateTime;

use crate::{Database, ReadinessError, check_readiness, runtime_lease::RuntimeLease};

const TELEGRAM_INGRESS_LOCK: i64 = 72_104_623;
const WEEK_MICROS: i64 = 7 * 24 * 60 * 60 * 1_000_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TelegramPollCursor {
    pub offset: Option<i64>,
    pub reset: bool,
}

#[derive(Debug)]
pub enum TelegramCursorError {
    InvalidInput,
    InvalidState,
    Busy,
    Storage(ReadinessError),
    Database,
}

impl fmt::Display for TelegramCursorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidInput => "Telegram cursor input is invalid",
            Self::InvalidState => "Saved Telegram cursor is invalid",
            Self::Busy => "Telegram ingress state is busy",
            Self::Storage(_) => "Telegram ingress storage is unavailable",
            Self::Database => "Telegram ingress state could not be accessed",
        })
    }
}
impl Error for TelegramCursorError {}
impl From<sqlx::Error> for TelegramCursorError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

#[derive(Deserialize)]
struct OffsetRecord {
    offset: Option<i64>,
    received_at: Option<String>,
}

#[derive(Deserialize)]
struct OrderingRecord {
    epoch: i64,
    last_received_at: String,
}

impl Database {
    /// Read the saved offset before polling. Telegram may choose a lower identifier
    /// after seven days without ingress, so discard a stale or undated offset.
    pub async fn telegram_poll_cursor(
        &self,
        instance: &str,
        now: StoredInstant,
    ) -> Result<TelegramPollCursor, TelegramCursorError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let txn = self.work.begin().await?;
            read_poll_cursor(txn, instance, now).await
        })
        .await
        .map_err(|_| TelegramCursorError::Database)?
    }
}

impl RuntimeLease {
    /// Read or reset the poll cursor on the session that holds the runtime lease.
    pub async fn owned_telegram_poll_cursor(
        &mut self,
        instance: &str,
        now: StoredInstant,
    ) -> Result<TelegramPollCursor, TelegramCursorError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let txn = self.connection.begin().await?;
            read_poll_cursor(txn, instance, now).await
        })
        .await
        .map_err(|_| TelegramCursorError::Database)?
    }
}

async fn read_poll_cursor(
    mut txn: Transaction<'_, Postgres>,
    instance: &str,
    now: StoredInstant,
) -> Result<TelegramPollCursor, TelegramCursorError> {
    let key = state_key("telegram:offset", instance)?;
    let ordering_key = state_key("telegram:ordering", instance)?;
    sqlx::query("SET LOCAL statement_timeout='750ms'")
        .execute(&mut *txn)
        .await?;
    lock_ingress(&mut txn).await?;
    let mut cursor = TelegramPollCursor {
        offset: None,
        reset: false,
    };
    if let Some(record) = read_state::<OffsetRecord>(&mut txn, &key).await?
        && let Some(offset) = record.offset
    {
        if offset < 1 {
            return Err(TelegramCursorError::InvalidState);
        }
        let received = match record.received_at {
            Some(at) => Some(parse_time(&at)?),
            None => read_state::<OrderingRecord>(&mut txn, &ordering_key)
                .await?
                .map(|record| parse_time(&record.last_received_at))
                .transpose()?,
        };
        cursor.reset = received.is_none_or(|received| week_elapsed(now, received));
        if cursor.reset {
            write_state(&mut txn, &key, json!({ "offset": null })).await?;
        } else {
            cursor.offset = Some(offset);
        }
    }
    txn.commit().await?;
    Ok(cursor)
}

/// Acknowledge one verified polling update after its accepted mutation has been
/// written in this same transaction. Updates from other chats also move the bot's cursor.
pub async fn advance_telegram_poll_cursor(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
    provider_update_id: i64,
    received_at: StoredInstant,
) -> Result<(), TelegramCursorError> {
    let key = state_key("telegram:offset", instance)?;
    let offset = provider_update_id
        .checked_add(1)
        .filter(|offset| *offset > 1)
        .ok_or(TelegramCursorError::InvalidInput)?;
    lock_ingress(txn).await?;
    write_state(
        txn,
        &key,
        json!({ "offset": offset, "received_at": received_at.to_string() }),
    )
    .await
}

/// Record the ordering epoch of a newly accepted owner update. Call after
/// transport deduplication, in the same transaction as its inbox event.
pub async fn record_telegram_ordering(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
    received_at: StoredInstant,
) -> Result<i64, TelegramCursorError> {
    let key = state_key("telegram:ordering", instance)?;
    lock_ingress(txn).await?;
    let (mut epoch, previous) = match read_state::<OrderingRecord>(txn, &key).await? {
        Some(record) if record.epoch >= 0 => {
            (record.epoch, Some(parse_time(&record.last_received_at)?))
        }
        Some(_) => return Err(TelegramCursorError::InvalidState),
        None if instance == "primary" => {
            let previous: Option<OffsetDateTime> =
                sqlx::query_scalar("SELECT max(received_at) FROM telegram_updates")
                    .fetch_one(&mut **txn)
                    .await?;
            let previous = previous
                .map(StoredInstant::try_from)
                .transpose()
                .map_err(|_| TelegramCursorError::InvalidState)?;
            (0, previous)
        }
        None => (0, None),
    };
    if previous.is_some_and(|previous| week_elapsed(received_at, previous)) {
        epoch = epoch
            .checked_add(1)
            .ok_or(TelegramCursorError::InvalidState)?;
    }
    write_state(
        txn,
        &key,
        json!({
            "epoch": epoch, "last_received_at": received_at.to_string(),
        }),
    )
    .await?;
    Ok(epoch)
}

pub(crate) async fn telegram_ordering_epoch(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
) -> Result<i64, TelegramCursorError> {
    let key = state_key("telegram:ordering", instance)?;
    match read_state::<OrderingRecord>(txn, &key).await? {
        Some(record) if record.epoch >= 0 => Ok(record.epoch),
        Some(_) => Err(TelegramCursorError::InvalidState),
        None => Ok(0),
    }
}

fn state_key(key: &str, instance: &str) -> Result<String, TelegramCursorError> {
    if !(1..=200).contains(&instance.chars().count()) || instance.contains('\0') {
        return Err(TelegramCursorError::InvalidInput);
    }
    if instance == "primary" {
        Ok(key.to_owned())
    } else {
        Ok(format!("{key}:telegram:{instance}"))
    }
}

fn week_elapsed(now: StoredInstant, since: StoredInstant) -> bool {
    now.unix_microseconds() - since.unix_microseconds() >= WEEK_MICROS
}

fn parse_time(value: &str) -> Result<StoredInstant, TelegramCursorError> {
    value.parse().map_err(|_| TelegramCursorError::InvalidState)
}

pub(crate) async fn lock_ingress(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), TelegramCursorError> {
    check_readiness(txn)
        .await
        .map_err(TelegramCursorError::Storage)?;
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(TELEGRAM_INGRESS_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(TelegramCursorError::Busy);
    }
    Ok(())
}

async fn read_state<T: serde::de::DeserializeOwned>(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
) -> Result<Option<T>, TelegramCursorError> {
    let value: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(key)
            .fetch_optional(&mut **txn)
            .await?;
    value
        .as_deref()
        .map(serde_json::from_str)
        .transpose()
        .map_err(|_| TelegramCursorError::InvalidState)
}

async fn write_state(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    value: serde_json::Value,
) -> Result<(), TelegramCursorError> {
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
