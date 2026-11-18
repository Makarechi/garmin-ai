//! Owner pause and resume commands shared with the existing Telegram and web state.

mod budget;
pub use budget::NotificationExclusions;

use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness, diary::DiaryWriteError,
    share_policy::OWNER_ONBOARDING_LOCK,
};

#[derive(Clone, Copy)]
pub enum ProactiveCommand {
    Telegram {
        message_at: StoredInstant,
        received_at: StoredInstant,
        ordering_epoch: i64,
        update_id: i64,
    },
    Web {
        received_at: StoredInstant,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProactiveControl {
    pub enabled: bool,
    pub changed: bool,
}

#[derive(Debug)]
pub enum ProactiveControlError {
    Storage(ReadinessError),
    UnknownOwner,
    InvalidCommand,
    InvalidStoredState,
    Busy,
    Database,
}

impl fmt::Display for ProactiveControlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Owner control storage is unavailable",
            Self::UnknownOwner => "Proactive control requires the enrolled owner",
            Self::InvalidCommand => "Owner control command is invalid",
            Self::InvalidStoredState => "Saved owner control is invalid",
            Self::Busy => "Owner control is being used",
            Self::Database => "Owner control could not be saved",
        })
    }
}
impl Error for ProactiveControlError {}
impl From<sqlx::Error> for ProactiveControlError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}
impl From<DiaryWriteError> for ProactiveControlError {
    fn from(error: DiaryWriteError) -> Self {
        match error {
            DiaryWriteError::Storage(error) => Self::Storage(error),
            DiaryWriteError::UnknownOwner => Self::UnknownOwner,
            DiaryWriteError::Busy => Self::Busy,
            _ => Self::Database,
        }
    }
}

impl Database {
    /// Save an authenticated owner's channel control. The caller supplies
    /// timestamps and ordering from trusted ingress metadata, never the model.
    pub async fn set_proactive_control(
        &self,
        owner_id: Uuid,
        command: ProactiveCommand,
        enabled: bool,
    ) -> Result<ProactiveControl, ProactiveControlError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = match command {
                ProactiveCommand::Web { .. } => self.begin_diary_write(owner_id).await?,
                ProactiveCommand::Telegram { .. } => {
                    let mut txn = self.control.begin().await?;
                    sqlx::query("SET LOCAL statement_timeout='750ms'")
                        .execute(&mut *txn)
                        .await?;
                    check_readiness(&mut txn)
                        .await
                        .map_err(ProactiveControlError::Storage)?;
                    let owner: bool =
                        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM people WHERE id=$1)")
                            .bind(owner_id)
                            .fetch_one(&mut *txn)
                            .await?;
                    if !owner {
                        return Err(ProactiveControlError::UnknownOwner);
                    }
                    txn
                }
            };
            let result = set_in(&mut txn, command, enabled).await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| ProactiveControlError::Database)?
    }
}

pub(crate) async fn set_in(
    txn: &mut Transaction<'_, Postgres>,
    command: ProactiveCommand,
    enabled: bool,
) -> Result<ProactiveControl, ProactiveControlError> {
    lock_policy(txn).await?;
    let previous: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='proactive:enabled'")
            .fetch_optional(&mut **txn)
            .await?;
    let previous: Option<Value> = previous
        .as_deref()
        .map(serde_json::from_str)
        .transpose()
        .map_err(|_| ProactiveControlError::InvalidStoredState)?;
    let (value, apply) = match command {
        ProactiveCommand::Telegram {
            message_at,
            received_at,
            ordering_epoch,
            update_id,
        } => {
            if ordering_epoch < 0 || update_id <= 0 {
                return Err(ProactiveControlError::InvalidCommand);
            }
            let message_at = message_at.unix_microseconds() / 1_000_000;
            let micros = received_at.unix_microseconds().rem_euclid(1_000_000);
            let at_us = message_at * 1_000_000 + micros;
            let incoming = (message_at, ordering_epoch, update_id);
            let apply = match previous.as_ref() {
                None => true,
                Some(old) if is_web(old) => {
                    timestamp_us(old)? < at_us && order(old, "telegram_", -1)? < incoming
                }
                Some(old) => {
                    // Existing records written before source tracking are Telegram.
                    old.get("source")
                        .is_none_or(|source| source == "telegram" || source.is_null())
                        && order(old, "", 0)? < incoming
                }
            };
            (
                json!({"enabled":enabled,"source":"telegram","message_at":message_at,
                    "message_at_us": at_us,"ordering_epoch":ordering_epoch,"update_id":update_id}),
                apply,
            )
        }
        ProactiveCommand::Web { received_at } => {
            let at_us = received_at.unix_microseconds();
            let message_at = at_us / 1_000_000;
            let (last_telegram, apply) = if let Some(old) = previous.as_ref() {
                let prefix = if is_web(old) { "telegram_" } else { "" };
                (
                    order(old, prefix, -1)?,
                    (
                        timestamp_us(old)?,
                        stored_int(old, "ordering_epoch", 0)?,
                        stored_int(old, "update_id", -1)?,
                    ) < (at_us, 0, 0),
                )
            } else {
                ((-1, -1, -1), true)
            };
            (
                json!({"enabled":enabled,"source":"web","message_at":message_at,
                    "message_at_us":at_us,"ordering_epoch":0,"update_id":0,
                    "telegram_message_at":last_telegram.0,"telegram_ordering_epoch":last_telegram.1,
                    "telegram_update_id":last_telegram.2}),
                apply,
            )
        }
    };
    let effective = if apply {
        sqlx::query(
            "INSERT INTO app_state(key,value) VALUES('proactive:enabled',$1::jsonb)
             ON CONFLICT (key) DO UPDATE SET value=excluded.value",
        )
        .bind(value.to_string())
        .execute(&mut **txn)
        .await?;
        enabled
    } else {
        previous
            .as_ref()
            .and_then(|value| value["enabled"].as_bool())
            .ok_or(ProactiveControlError::InvalidStoredState)?
    };
    if !effective {
        cancel_pending(txn, matches!(command, ProactiveCommand::Telegram { .. })).await?;
    }
    Ok(ProactiveControl {
        enabled: effective,
        changed: apply,
    })
}

async fn lock_policy(txn: &mut Transaction<'_, Postgres>) -> Result<(), ProactiveControlError> {
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(OWNER_ONBOARDING_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if locked {
        Ok(())
    } else {
        Err(ProactiveControlError::Busy)
    }
}

fn is_web(value: &Value) -> bool {
    value["source"].as_str() == Some("web")
}

fn order(
    value: &Value,
    prefix: &str,
    default_epoch: i64,
) -> Result<(i64, i64, i64), ProactiveControlError> {
    Ok((
        stored_int(value, &format!("{prefix}message_at"), -1)?,
        stored_int(value, &format!("{prefix}ordering_epoch"), default_epoch)?,
        stored_int(value, &format!("{prefix}update_id"), -1)?,
    ))
}

fn timestamp_us(value: &Value) -> Result<i64, ProactiveControlError> {
    if !value["message_at_us"].is_null() {
        return stored_int(value, "message_at_us", -1);
    }
    stored_int(value, "message_at", -1)?
        .checked_mul(1_000_000)
        .ok_or(ProactiveControlError::InvalidStoredState)
}

fn stored_int(value: &Value, key: &str, default: i64) -> Result<i64, ProactiveControlError> {
    match &value[key] {
        Value::Null => Ok(default),
        Value::String(value) => value
            .trim()
            .parse()
            .map_err(|_| ProactiveControlError::InvalidStoredState),
        value => value
            .as_i64()
            .ok_or(ProactiveControlError::InvalidStoredState),
    }
}

async fn cancel_pending(
    txn: &mut Transaction<'_, Postgres>,
    telegram: bool,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE outbox_messages SET state='cancelled',next_attempt_at=NULL,updated_at=now()
         WHERE state='queued' AND intent->'initiative'='true'::jsonb",
    )
    .execute(&mut **txn)
    .await?;
    sqlx::query(
        "UPDATE pending_questions SET status='cancelled',
           evidence=evidence||'{\"cancel_reason\":\"owner_pause\"}'::jsonb,
           sent_at=CASE WHEN $1 THEN NULL ELSE sent_at END
         WHERE (status='pending' AND sent_at IS NULL) OR status='sending'",
    )
    .bind(telegram)
    .execute(&mut **txn)
    .await?;
    if telegram {
        sqlx::query(
            "UPDATE insights SET status='cancelled',
             evidence=evidence||'{\"cancel_reason\":\"owner_pause\"}'::jsonb
             WHERE status='accepted'",
        )
        .execute(&mut **txn)
        .await?;
        sqlx::query(
            "DELETE FROM app_state WHERE key LIKE 'insight:last:%'
             AND CASE jsonb_typeof(value->'reservation')
                WHEN 'boolean' THEN value->'reservation' = 'true'::jsonb
                WHEN 'string' THEN value->>'reservation' <> ''
                WHEN 'object' THEN value->'reservation' <> '{}'::jsonb
                WHEN 'array' THEN value->'reservation' <> '[]'::jsonb
                WHEN 'number' THEN value->'reservation' <> '0'::jsonb
                ELSE false END",
        )
        .execute(&mut **txn)
        .await?;
    }
    Ok(())
}
