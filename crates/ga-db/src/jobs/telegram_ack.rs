use std::future::Future;

use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{ATTEMPT_LIMIT, JobOutcome, JobStoreError, RecordedJobStatus, begin, fail_exhausted};
use crate::{
    Database,
    channel_ingress::telegram_poll::{TelegramPollError, selection},
    telegram_binding::{TelegramBindingError, confirmed_pairing},
};

const KIND: &str = "telegram_native_ack";
const LEASE_SECONDS: i32 = 15;

pub enum TelegramAckAuthorization<T> {
    BindingChanged,
    Busy,
    Observed(T),
}

#[derive(Clone, Copy)]
pub struct TelegramAckTarget<'a> {
    pub owner_id: Uuid,
    pub instance_id: &'a str,
    pub confirmed_chat: Option<i64>,
}

pub struct TelegramAckLease {
    id: Uuid,
    token: Uuid,
    callback_id: String,
}

impl TelegramAckLease {
    #[must_use]
    pub fn callback_id(&self) -> &str {
        &self.callback_id
    }
}

/// Persist an acknowledgement alongside the authenticated update. The legacy
/// Python acknowledgement job reads a different payload from its shadow inbox.
pub async fn enqueue_telegram_ack(
    txn: &mut Transaction<'_, Postgres>,
    target: TelegramAckTarget<'_>,
    update_id: i64,
    callback_id: &str,
    received_at: StoredInstant,
) -> Result<bool, JobStoreError> {
    if !valid_instance(target.instance_id)
        || target
            .confirmed_chat
            .is_some_and(|chat| !(1..(1_i64 << 52)).contains(&chat))
        || !(1..i64::MAX).contains(&update_id)
        || !valid_callback_id(callback_id)
    {
        return Err(JobStoreError::InvalidRecordedState);
    }
    let key = format!(
        "telegram:ack:native:{}:{}:{update_id}",
        target.owner_id, target.instance_id
    );
    let result = sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES($1,$2,$3::jsonb,$4,'pending',$5,0)
         ON CONFLICT(dedup_key) DO NOTHING",
    )
    .bind(Uuid::new_v4())
    .bind(KIND)
    .bind(
        json!({
            "owner_id":target.owner_id.to_string(),
            "instance_id":target.instance_id,
            "confirmed_chat":target.confirmed_chat,
            "update_id":update_id,
            "callback_id":callback_id,
        })
        .to_string(),
    )
    .bind(key)
    .bind(OffsetDateTime::from(received_at))
    .execute(&mut **txn)
    .await?;
    Ok(result.rows_affected() == 1)
}

impl Database {
    /// Claim acknowledgement work on the short control path so a long poll
    /// can wait on a separate connection without delaying the callback.
    pub async fn claim_telegram_ack(
        &self,
        target: TelegramAckTarget<'_>,
        now: StoredInstant,
    ) -> Result<Option<TelegramAckLease>, JobStoreError> {
        if !valid_instance(target.instance_id) {
            return Err(JobStoreError::InvalidRecordedState);
        }
        let mut txn = begin(&self.control).await?;
        let now = OffsetDateTime::from(now);
        fail_exhausted(&mut txn, now, Some(&[KIND])).await?;
        let token = Uuid::new_v4();
        let found: Option<(Uuid, Option<String>)> = sqlx::query_as(
            "WITH eligible AS (
                SELECT id FROM jobs
                WHERE kind=$1 AND payload->>'owner_id'=$2 AND payload->>'instance_id'=$3
                  AND attempts<$4 AND (
                       (status='pending' AND run_at<=$5)
                    OR (status='running' AND lease_until<$5))
                ORDER BY run_at,id FOR UPDATE SKIP LOCKED LIMIT 1
             )
             UPDATE jobs SET status='running', attempts=attempts+1,
                   lease_until=$5 + $6 * interval '1 second', lease_token=$7
             FROM eligible WHERE jobs.id=eligible.id RETURNING jobs.id,jobs.payload->>'callback_id'",
        )
        .bind(KIND)
        .bind(target.owner_id.to_string())
        .bind(target.instance_id)
        .bind(ATTEMPT_LIMIT)
        .bind(now)
        .bind(LEASE_SECONDS)
        .bind(token)
        .fetch_optional(&mut *txn)
        .await?;
        let found = match found {
            Some((id, callback_id)) => {
                let callback_id = callback_id
                    .filter(|id| valid_callback_id(id))
                    .ok_or(JobStoreError::InvalidRecordedState)?;
                Some(TelegramAckLease {
                    id,
                    token,
                    callback_id,
                })
            }
            None => None,
        };
        txn.commit().await?;
        Ok(found)
    }

    pub async fn finish_telegram_ack(
        &self,
        lease: &TelegramAckLease,
        now: StoredInstant,
        outcome: JobOutcome,
    ) -> Result<RecordedJobStatus, JobStoreError> {
        self.finish_job(lease.id, lease.token, KIND, now, outcome)
            .await
    }

    /// Keep the same pairing and channel selection for the full provider request.
    pub async fn with_telegram_ack_authorization<T>(
        &self,
        lease: &TelegramAckLease,
        target: TelegramAckTarget<'_>,
        request: impl Future<Output = T>,
    ) -> Result<TelegramAckAuthorization<T>, JobStoreError> {
        let Some(chat) = target.confirmed_chat else {
            return Ok(TelegramAckAuthorization::BindingChanged);
        };
        let mut txn = begin(&self.control).await?;
        let stored: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT payload->>'confirmed_chat' FROM jobs
             WHERE id=$1 AND kind=$2 AND status='running' AND lease_token=$3
               AND lease_until>clock_timestamp() + interval '6 seconds' AND payload->>'owner_id'=$4
               AND payload->>'instance_id'=$5 FOR UPDATE NOWAIT",
        )
        .bind(lease.id)
        .bind(KIND)
        .bind(lease.token)
        .bind(target.owner_id.to_string())
        .bind(target.instance_id)
        .fetch_optional(&mut *txn)
        .await?;
        let (stored,) = stored.ok_or(JobStoreError::LeaseLost)?;
        if stored.as_deref() != Some(&chat.to_string()) {
            return Ok(TelegramAckAuthorization::BindingChanged);
        }
        match selection(&mut txn, target.instance_id).await {
            Ok(true) => {}
            Ok(false) => return Ok(TelegramAckAuthorization::BindingChanged),
            Err(TelegramPollError::Busy) => return Ok(TelegramAckAuthorization::Busy),
            Err(_) => return Err(JobStoreError::Database),
        }
        match confirmed_pairing(&mut txn, target.instance_id, chat).await {
            Ok(owner) if owner == target.owner_id => {}
            Ok(_) | Err(TelegramBindingError::Missing | TelegramBindingError::Mismatch) => {
                return Ok(TelegramAckAuthorization::BindingChanged);
            }
            Err(TelegramBindingError::Busy) => {
                return Ok(TelegramAckAuthorization::Busy);
            }
            Err(_) => return Err(JobStoreError::Database),
        }
        let result = request.await;
        txn.rollback().await?;
        Ok(TelegramAckAuthorization::Observed(result))
    }
}

fn valid_instance(value: &str) -> bool {
    (1..=200).contains(&value.chars().count()) && !value.contains('\0')
}

fn valid_callback_id(value: &str) -> bool {
    (1..=1000).contains(&value.len()) && !value.contains('\0')
}
