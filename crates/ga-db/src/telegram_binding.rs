use std::{error::Error, fmt, time::Duration};

use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness};

const CHANNEL_BINDING_LOCK: i64 = 72_104_627;

#[derive(Debug)]
pub enum TelegramBindingError {
    Missing,
    Mismatch,
    Busy,
    Storage(ReadinessError),
    Database,
}

impl fmt::Display for TelegramBindingError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Missing => "Telegram owner pairing is required",
            Self::Mismatch => "Telegram account does not match its recorded pairing",
            Self::Busy => "Telegram ownership is being updated",
            Self::Storage(_) => "Telegram ownership storage is unavailable",
            Self::Database => "Telegram owner pairing could not be read",
        })
    }
}
impl Error for TelegramBindingError {}
impl From<sqlx::Error> for TelegramBindingError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Resolve an existing Telegram pairing for the database's sole owner.
    pub async fn confirmed_telegram_owner(
        &self,
        instance: &str,
        expected_chat: i64,
    ) -> Result<Uuid, TelegramBindingError> {
        if !(1..=200).contains(&instance.chars().count())
            || instance.contains('\0')
            || !(1..(1_i64 << 52)).contains(&expected_chat)
        {
            return Err(TelegramBindingError::Mismatch);
        }
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin().await?;
            sqlx::query("SET LOCAL statement_timeout = '750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(TelegramBindingError::Storage)?;
            let owner = confirmed_pairing(&mut txn, instance, expected_chat).await?;
            txn.commit().await?;
            Ok(owner)
        })
        .await
        .map_err(|_| TelegramBindingError::Database)?
    }
}

pub(crate) async fn confirmed_pairing(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
    expected_chat: i64,
) -> Result<Uuid, TelegramBindingError> {
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
        .bind(CHANNEL_BINDING_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(TelegramBindingError::Busy);
    }
    let owners: Vec<Uuid> = sqlx::query_scalar("SELECT id FROM people LIMIT 2")
        .fetch_all(&mut **txn)
        .await?;
    let owner = match owners.as_slice() {
        [] => return Err(TelegramBindingError::Missing),
        [owner] => *owner,
        _ => return Err(TelegramBindingError::Mismatch),
    };
    let chat: Option<String> = sqlx::query_scalar(
        "SELECT external_id FROM channel_bindings
         WHERE owner_id=$1 AND channel='telegram' AND channel_instance_id=$2
           AND confirmed_at IS NOT NULL",
    )
    .bind(owner)
    .bind(instance)
    .fetch_optional(&mut **txn)
    .await?;
    let chat = chat.ok_or(TelegramBindingError::Missing)?;
    if chat != expected_chat.to_string() {
        return Err(TelegramBindingError::Mismatch);
    }
    Ok(owner)
}
