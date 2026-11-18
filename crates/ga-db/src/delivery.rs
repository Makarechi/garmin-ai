use std::{error::Error, fmt, time::Duration};

use ga_domain::{
    access::{Scope, Scopes},
    timestamp::StoredInstant,
};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness, jobs::storage_notices::StorageNoticeFence,
    replay::REPLAY_MUTATION_LOCK,
};

const RECOVERY_BATCH: i64 = 100;

#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeliveryState {
    Queued,
    Sending,
    ProviderAccepted,
    Delivered,
    Read,
    Failed,
    Uncertain,
    Cancelled,
    Expired,
}

impl DeliveryState {
    pub fn label(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Sending => "sending",
            Self::ProviderAccepted => "provider_accepted",
            Self::Delivered => "delivered",
            Self::Read => "read",
            Self::Failed => "failed",
            Self::Uncertain => "uncertain",
            Self::Cancelled => "cancelled",
            Self::Expired => "expired",
        }
    }

    pub fn confirms_delivery(self) -> bool {
        matches!(self, Self::Delivered | Self::Read)
    }

    pub fn confirms_read(self) -> bool {
        self == Self::Read
    }

    fn advances(self, prior: &str) -> bool {
        let rank = match prior {
            "queued" => 0,
            "sending" => 1,
            "provider_accepted" => 2,
            "delivered" => 3,
            "read" => 4,
            _ => -1,
        };
        match self {
            Self::Queued | Self::Sending if prior == "uncertain" => false,
            Self::Queued => rank < 0,
            Self::Sending => rank < 1,
            Self::ProviderAccepted => rank < 2,
            Self::Delivered | Self::Failed | Self::Uncertain => rank < 3,
            Self::Read => rank < 4,
            Self::Cancelled | Self::Expired => rank < 2,
        }
    }
}

pub struct DeliveryReceipt<'a> {
    pub id: Uuid,
    pub intent_id: Uuid,
    pub state: DeliveryState,
    pub observed_at: StoredInstant,
    pub provider_reference: Option<&'a str>,
    pub detail: Option<&'a str>,
}

#[derive(Clone, Copy)]
pub struct DeliveryTarget {
    pub owner_id: Uuid,
    pub outbox_id: Uuid,
    pub lease_token: Option<Uuid>,
}

#[derive(Debug)]
pub enum DeliveryError {
    Storage(ReadinessError),
    InvalidReceipt,
    MissingIntent,
    LeaseReplaced,
    RequiresReconciliation,
    Forbidden,
    Busy,
    Database,
}

impl fmt::Display for DeliveryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Outbox storage is unavailable",
            Self::InvalidReceipt => "Delivery receipt is invalid",
            Self::MissingIntent => "Outbox message does not match receipt",
            Self::LeaseReplaced => "Outbox lease was replaced before delivery completed",
            Self::RequiresReconciliation => "Only an uncertain delivery can be explicitly requeued",
            Self::Forbidden => "Explicit delivery reconciliation permission required",
            Self::Busy => "Outbox storage is busy",
            Self::Database => "Outbox state could not be stored",
        })
    }
}
impl Error for DeliveryError {}
impl From<sqlx::Error> for DeliveryError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

/// Persist observed evidence and reconcile the outbox state in the caller's transaction.
/// The caller verifies the provider identity and routing before passing a receipt.
pub async fn record_delivery_receipt(
    txn: &mut Transaction<'_, Postgres>,
    target: DeliveryTarget,
    receipt: DeliveryReceipt<'_>,
) -> Result<Uuid, DeliveryError> {
    if target.outbox_id != receipt.intent_id {
        return Err(DeliveryError::MissingIntent);
    }
    if receipt
        .provider_reference
        .is_some_and(|value| !(1..=1000).contains(&value.chars().count()))
        || receipt
            .detail
            .is_some_and(|value| value.chars().count() > 2000)
    {
        return Err(DeliveryError::InvalidReceipt);
    }
    check_readiness(txn).await.map_err(DeliveryError::Storage)?;
    let (prior, lease_token): (String, Option<Uuid>) = sqlx::query_as(
        "SELECT state,lease_token FROM outbox_messages WHERE id=$1 AND owner_id=$2 FOR UPDATE",
    )
    .bind(target.outbox_id)
    .bind(target.owner_id)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(DeliveryError::MissingIntent)?;
    if target
        .lease_token
        .is_some_and(|token| lease_token != Some(token))
    {
        return Err(DeliveryError::LeaseReplaced);
    }

    let observed_at = OffsetDateTime::from(receipt.observed_at);
    let evidence_id = sqlx::query_scalar(
        "INSERT INTO message_delivery_receipts(id,outbox_message_id,state,observed_at,provider_reference,detail)
         VALUES($1,$2,$3,$4,$5,$6)
         ON CONFLICT ON CONSTRAINT uq_delivery_receipt_evidence DO NOTHING RETURNING id",
    )
    .bind(receipt.id)
    .bind(target.outbox_id)
    .bind(receipt.state.label())
    .bind(observed_at)
    .bind(receipt.provider_reference)
    .bind(receipt.detail)
    .fetch_optional(&mut **txn)
    .await?;
    let evidence_id = match evidence_id {
        Some(id) => id,
        None => sqlx::query_scalar(
            "SELECT id FROM message_delivery_receipts WHERE outbox_message_id=$1 AND state=$2 AND observed_at=$3",
        )
        .bind(target.outbox_id)
        .bind(receipt.state.label())
        .bind(observed_at)
        .fetch_one(&mut **txn)
        .await?,
    };
    let latest: OffsetDateTime = sqlx::query_scalar(
        "SELECT max(observed_at) FROM message_delivery_receipts WHERE outbox_message_id=$1",
    )
    .bind(target.outbox_id)
    .fetch_one(&mut **txn)
    .await?;
    if observed_at < latest {
        return Ok(evidence_id);
    }
    let state = receipt
        .state
        .advances(&prior)
        .then_some(receipt.state.label());
    sqlx::query(
        "UPDATE outbox_messages SET state=coalesce($2,state),provider_reference=coalesce($3,provider_reference),
              lease_token=CASE WHEN $4 THEN NULL ELSE lease_token END,
              lease_until=CASE WHEN $4 THEN NULL ELSE lease_until END,
              updated_at=clock_timestamp()
         WHERE id=$1 AND (
             state IS DISTINCT FROM coalesce($2,state)
             OR provider_reference IS DISTINCT FROM coalesce($3,provider_reference)
             OR ($4 AND (lease_token IS NOT NULL OR lease_until IS NOT NULL))
         )",
    )
    .bind(target.outbox_id)
    .bind(state)
    .bind(receipt.provider_reference)
    .bind(receipt.state != DeliveryState::Sending)
    .execute(&mut **txn)
    .await?;
    if receipt.state.confirms_delivery() {
        let mut savepoint = txn.begin().await?;
        match crate::conversation_memory::remember_delivered_in(
            &mut savepoint,
            target.owner_id,
            target.outbox_id,
        )
        .await
        {
            Ok(_) => savepoint.commit().await?,
            Err(_) => savepoint.rollback().await?,
        }
    }
    Ok(evidence_id)
}

impl Database {
    /// Expired in-flight sends need evidence or explicit reconciliation before a resend.
    pub async fn recover_expired_outbox(&self, now: StoredInstant) -> Result<u64, DeliveryError> {
        self.recover_expired_replies(None, now).await
    }

    /// An active Telegram worker may recover only its own native non-legacy sends.
    pub async fn recover_expired_telegram_outbox(
        &self,
        destination: &crate::outbox_claims::ReplyDestination<'_>,
        now: StoredInstant,
    ) -> Result<u64, DeliveryError> {
        self.recover_expired_replies(Some(destination), now).await
    }

    async fn recover_expired_replies(
        &self,
        destination: Option<&crate::outbox_claims::ReplyDestination<'_>>,
        now: StoredInstant,
    ) -> Result<u64, DeliveryError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.work.begin().await?;
            prepare_write(&mut txn).await?;
            let result = sqlx::query(
                "WITH expired AS (
                    SELECT id FROM outbox_messages o WHERE state='sending' AND lease_until<$1
                      AND ($3::uuid IS NULL OR (owner_id=$3 AND legacy_key IS NULL
                        AND EXISTS(SELECT FROM conversations c WHERE c.id=o.conversation_id
                              AND c.owner_id=$3 AND c.channel='telegram' AND c.channel_instance_id=$4)))
                    ORDER BY lease_until,id LIMIT $2 FOR UPDATE SKIP LOCKED
                 )
                 UPDATE outbox_messages SET state='uncertain',lease_until=NULL,lease_token=NULL,
                     updated_at=clock_timestamp() FROM expired WHERE outbox_messages.id=expired.id",
            )
            .bind(OffsetDateTime::from(now))
            .bind(RECOVERY_BATCH)
            .bind(destination.map(|target| target.owner_id))
            .bind(destination.map(|target| target.instance_id))
            .execute(&mut *txn)
            .await?;
            txn.commit().await?;
            Ok(result.rows_affected())
        })
        .await
        .map_err(|_| DeliveryError::Database)?
    }

    pub async fn requeue_uncertain_delivery(
        &self,
        scopes: Scopes,
        owner_id: Uuid,
        outbox_id: Uuid,
    ) -> Result<(), DeliveryError> {
        if !scopes.permits(&[Scope::ManageIntegrations]) {
            return Err(DeliveryError::Forbidden);
        }
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.work.begin().await?;
            prepare_write(&mut txn).await?;
            let (state, fence, instance): (String, Option<String>, Option<String>) = sqlx::query_as(
                "SELECT state,memory_fence::text,
                    CASE WHEN intent#>>'{channel_instance,channel}'='telegram'
                      THEN intent#>>'{channel_instance,instance_id}' END
                 FROM outbox_messages WHERE id=$1 AND owner_id=$2 FOR UPDATE",
            )
            .bind(outbox_id)
            .bind(owner_id)
            .fetch_optional(&mut *txn)
            .await?
            .ok_or(DeliveryError::MissingIntent)?;
            if state != DeliveryState::Uncertain.label() {
                return Err(DeliveryError::RequiresReconciliation);
            }
            if let Some(notice) = fence.as_deref().and_then(|value| serde_json::from_str::<StorageNoticeFence>(value).ok())
                && !notice.reopen(&mut txn, instance.as_deref().ok_or(DeliveryError::InvalidReceipt)?).await?
            {
                return Err(DeliveryError::RequiresReconciliation);
            }
            sqlx::query("UPDATE outbox_messages SET state='queued',next_attempt_at=NULL,updated_at=clock_timestamp() WHERE id=$1")
                .bind(outbox_id)
                .execute(&mut *txn)
                .await?;
            txn.commit().await?;
            Ok(())
        })
        .await
        .map_err(|_| DeliveryError::Database)?
    }
}

async fn prepare_write(txn: &mut Transaction<'_, Postgres>) -> Result<(), DeliveryError> {
    sqlx::query("SET LOCAL statement_timeout = '750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn).await.map_err(DeliveryError::Storage)?;
    let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !acquired {
        return Err(DeliveryError::Busy);
    }
    Ok(())
}
