use std::{error::Error, fmt, time::Duration};

use ga_domain::{
    channel::{InboundEnvelope, InboundKind},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Acquire, Connection, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ChannelCallback, InboundReceipt, IngressError, callback::resolve_and_ingest, ingest,
    lock_mutations,
};
use crate::{
    check_readiness,
    runtime_lease::RuntimeLease,
    telegram_cursor::{
        TelegramCursorError, advance_telegram_poll_cursor, lock_ingress, record_telegram_ordering,
    },
};

const CHANNEL_SELECTION_LOCK: i64 = 72_104_621;

pub enum TelegramPollEvent<'a> {
    Ignore,
    Message(&'a InboundEnvelope),
    Callback(ChannelCallback<'a>),
}

pub struct TelegramPollInput<'a> {
    pub owner_id: Uuid,
    pub instance_id: &'a str,
    pub update_id: i64,
    pub received_at: StoredInstant,
    pub event: TelegramPollEvent<'a>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct TelegramPollReceipt {
    pub selected: bool,
    pub inbound: Option<InboundReceipt>,
    pub new_ordering_epoch: Option<i64>,
}

#[derive(Debug)]
pub enum TelegramPollError {
    InvalidBinding,
    InvalidSelection,
    Busy,
    Ingress(IngressError),
    Cursor(TelegramCursorError),
    Database,
}

impl fmt::Display for TelegramPollError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidBinding => {
                f.write_str("Telegram update does not match the polling binding")
            }
            Self::InvalidSelection => f.write_str("Saved Telegram channel selection is invalid"),
            Self::Busy => f.write_str("Telegram channel selection is changing"),
            Self::Ingress(error) => error.fmt(f),
            Self::Cursor(error) => error.fmt(f),
            Self::Database => f.write_str("Telegram poll update could not be committed"),
        }
    }
}
impl Error for TelegramPollError {}
impl From<sqlx::Error> for TelegramPollError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}
impl From<IngressError> for TelegramPollError {
    fn from(error: IngressError) -> Self {
        Self::Ingress(error)
    }
}
impl From<TelegramCursorError> for TelegramPollError {
    fn from(error: TelegramCursorError) -> Self {
        Self::Cursor(error)
    }
}

impl RuntimeLease {
    /// The inbox, button consumption, ordering record and offset commit on the
    /// same connection that holds the shared Python/Rust runtime advisory lock.
    pub async fn persist_telegram_poll_event(
        &mut self,
        input: TelegramPollInput<'_>,
        normalize_callback: impl FnOnce(&Value) -> Option<InboundEnvelope>,
    ) -> Result<TelegramPollReceipt, TelegramPollError> {
        validate_binding(&input)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = Connection::begin(&mut self.connection).await?;
            sqlx::query("SET LOCAL statement_timeout = '750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(IngressError::Storage)?;
            let selected = selection(&mut txn, input.instance_id).await?;
            lock_ingress(&mut txn).await?;
            // Legacy Telegram records an accepted edit as a revision without
            // advancing the timestamp of the last newly accepted message.
            let advances_ordering = !matches!(
                &input.event, TelegramPollEvent::Message(envelope) if envelope.kind() == InboundKind::Edit
            );
            let inbound = if selected {
                save_event(&mut txn, input.event, normalize_callback).await?
            } else {
                None
            };
            let new_ordering_epoch = if advances_ordering && inbound.is_some_and(|receipt| receipt.created) {
                Some(
                    record_telegram_ordering(&mut txn, input.instance_id, input.received_at)
                        .await?,
                )
            } else {
                None
            };
            advance_telegram_poll_cursor(
                &mut txn,
                input.instance_id,
                input.update_id,
                input.received_at,
            )
            .await?;
            txn.commit().await?;
            Ok(TelegramPollReceipt {
                selected,
                inbound,
                new_ordering_epoch,
            })
        })
        .await
        .map_err(|_| TelegramPollError::Database)?
    }
}

async fn selection(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
) -> Result<bool, TelegramPollError> {
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
        .bind(CHANNEL_SELECTION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(TelegramPollError::Busy);
    }
    let stored: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='preferences:onboarding'")
            .fetch_optional(&mut **txn)
            .await?;
    let Some(stored) = stored else {
        return Ok(true);
    };
    let stored: Value =
        serde_json::from_str(&stored).map_err(|_| TelegramPollError::InvalidSelection)?;
    if !stored.is_object() {
        return Err(TelegramPollError::InvalidSelection);
    }
    let channel = json!({"channel":"telegram","instance_id":instance});
    Ok(stored["channel"] == channel
        || stored["fallback_channels"]
            .as_array()
            .is_some_and(|fallbacks| fallbacks.contains(&channel)))
}

async fn save_event(
    txn: &mut Transaction<'_, Postgres>,
    event: TelegramPollEvent<'_>,
    normalize_callback: impl FnOnce(&Value) -> Option<InboundEnvelope>,
) -> Result<Option<InboundReceipt>, IngressError> {
    if matches!(event, TelegramPollEvent::Ignore) {
        return Ok(None);
    }
    lock_mutations(txn).await?;
    // A stale edit or rejected button should not leave earlier writes in this
    // transaction. Its provider offset must still advance.
    let mut event_txn = txn.begin().await?;
    let result = match event {
        TelegramPollEvent::Ignore => Ok(None),
        TelegramPollEvent::Message(envelope) => ingest(&mut event_txn, envelope).await.map(Some),
        TelegramPollEvent::Callback(callback) => {
            resolve_and_ingest(&mut event_txn, &callback, normalize_callback)
                .await
                .map(Some)
        }
    };
    match result {
        Ok(receipt) => {
            event_txn.commit().await?;
            Ok(receipt)
        }
        Err(
            IngressError::MissingRevision
            | IngressError::OldRevision
            | IngressError::ActionUnavailable
            | IngressError::InvalidEnvelope,
        ) => {
            event_txn.rollback().await?;
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn validate_binding(input: &TelegramPollInput<'_>) -> Result<(), TelegramPollError> {
    if !(1..i64::MAX).contains(&input.update_id)
        || !(1..=200).contains(&input.instance_id.chars().count())
        || input.instance_id.contains('\0')
    {
        return Err(TelegramPollError::InvalidBinding);
    }
    let event_id = input.update_id.to_string();
    let valid = match &input.event {
        TelegramPollEvent::Ignore => true,
        TelegramPollEvent::Message(envelope) => {
            envelope.owner_id() == input.owner_id
                && envelope.channel().channel() == "telegram"
                && envelope.channel().instance_id() == input.instance_id
                && envelope.external_event_id() == event_id
                && envelope.received_at() == OffsetDateTime::from(input.received_at)
                && envelope.kind() != InboundKind::Action
        }
        TelegramPollEvent::Callback(callback) => {
            callback.owner_id == input.owner_id
                && callback.channel == "telegram"
                && callback.instance_id == input.instance_id
                && callback.event_id == event_id
                && callback.received_at == input.received_at
        }
    };
    if !valid {
        return Err(TelegramPollError::InvalidBinding);
    }
    Ok(())
}
