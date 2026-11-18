use ga_domain::{
    channel::{OutboundIntent, telegram_conversation_id},
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{OffsetDateTime, Time};
use uuid::Uuid;

use super::{JobStoreError, begin};
use crate::{
    Database,
    channel_ingress::telegram_poll::{selection, telegram_primary},
    delivery::DeliveryState,
    outbox_enqueue::{QueueIntent, queue_outbox_intent},
    replay::REPLAY_MUTATION_LOCK,
    telegram_binding::confirmed_pairing,
};

const KIND: &str = "telegram_storage_notice";
const NOTICE: &str = "Мало свободного места для следующей резервной копии. Проверьте хранилище и запустите backup-space. Последняя копия не удалялась автоматически.";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct StorageNoticeFence {
    pub(crate) storage_notice_day: String,
}

impl StorageNoticeFence {
    fn key(&self) -> String {
        format!("storage-notice:{}", self.storage_notice_day)
    }

    fn receipt(&self) -> String {
        format!("outbox:{}:0", self.key())
    }

    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        now: StoredInstant,
    ) -> Result<bool, sqlx::Error> {
        if self.storage_notice_day != OffsetDateTime::from(now).date().to_string() {
            return Ok(false);
        }
        let (shortage, receipt): (bool, Option<String>) = sqlx::query_as(
            "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='storage:backup-capacity'
                                       AND value->>'status'='insufficient'),
                    (SELECT value::text FROM app_state WHERE key=$1)",
        )
        .bind(self.receipt())
        .fetch_one(&mut **txn)
        .await?;
        Ok(shortage
            && receipt.as_deref().is_none_or(|raw| {
                let Ok(receipt) = serde_json::from_str::<Value>(raw) else {
                    return false;
                };
                receipt["status"] == "pending"
                    && StoredInstant::from_legacy_input(&receipt["retry_at"])
                        .is_some_and(|retry_at| retry_at <= now)
            }))
    }

    pub(crate) async fn start(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        instance: &str,
        now: StoredInstant,
    ) -> Result<(), sqlx::Error> {
        self.write_receipt(
            txn,
            instance,
            json!({"status":"sending","started_at":now.to_string()}),
        )
        .await
    }

    pub(crate) async fn retry(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        instance: &str,
        at: StoredInstant,
    ) -> Result<(), sqlx::Error> {
        self.write_receipt(
            txn,
            instance,
            json!({"status":"pending","retry_at":at.to_string()}),
        )
        .await
    }

    pub(crate) async fn complete(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        instance: &str,
        state: DeliveryState,
        provider_reference: Option<&str>,
    ) -> Result<(), sqlx::Error> {
        let receipt = if matches!(
            state,
            DeliveryState::ProviderAccepted | DeliveryState::Delivered | DeliveryState::Read
        ) {
            json!({"status":"sent","message_id":provider_reference.and_then(|id| id.parse::<i64>().ok())})
        } else {
            json!({"status":"uncertain"})
        };
        self.write_receipt(txn, instance, receipt).await?;
        sqlx::query(
            "UPDATE jobs SET status='done',completed_at=clock_timestamp(),
                      lease_until=NULL,lease_token=NULL,last_error=NULL
                     WHERE dedup_key=$1 AND kind=$2 AND status='pending'",
        )
        .bind(self.key())
        .bind(KIND)
        .execute(&mut **txn)
        .await?;
        Ok(())
    }

    async fn write_receipt(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        instance: &str,
        mut receipt: Value,
    ) -> Result<(), sqlx::Error> {
        receipt["channel_instance_id"] = json!(format!("telegram:{instance}"));
        receipt["formatted"] = json!(true);
        sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp()")
            .bind(self.receipt()).bind(receipt.to_string()).execute(&mut **txn).await?;
        Ok(())
    }
}

pub(super) async fn schedule(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
) -> Result<(), sqlx::Error> {
    let at = OffsetDateTime::from(now);
    let fence = StorageNoticeFence {
        storage_notice_day: at.date().to_string(),
    };
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
                 VALUES($1,$2,$3::jsonb,$4,'pending',$5,0)
                 ON CONFLICT(dedup_key) DO UPDATE SET status='pending',run_at=excluded.run_at,
                    attempts=0,completed_at=NULL,last_error=NULL,lease_until=NULL,lease_token=NULL
                 WHERE jobs.kind=$2 AND jobs.status='done'
                 AND NOT EXISTS(SELECT 1 FROM app_state WHERE key=$6)",
    )
    .bind(Uuid::new_v4())
    .bind(KIND)
    .bind(json!({"day":fence.storage_notice_day}).to_string())
    .bind(fence.key())
    .bind(at)
    .bind(fence.receipt())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

impl Database {
    /// Move a due storage warning into the normal Telegram outbox.
    pub async fn queue_telegram_storage_notice(
        &self,
        owner: Uuid,
        instance: &str,
        chat: i64,
        now: StoredInstant,
    ) -> Result<(), JobStoreError> {
        let clock = OffsetDateTime::from(now);
        let fence = StorageNoticeFence {
            storage_notice_day: clock.date().to_string(),
        };
        let key = fence.key();
        let mut txn = begin(&self.work).await?;
        if !fence.current(&mut txn, now).await? {
            return Ok(());
        }
        let operation: Option<Uuid> = sqlx::query_scalar(
            "SELECT id FROM jobs WHERE kind=$1 AND dedup_key=$2 AND payload->>'day'=$3
                AND ((status='pending' AND run_at<=$4)
                  OR (status='running' AND lease_until<$4))
                AND NOT EXISTS(SELECT 1 FROM outbox_messages WHERE dedup_key=$2 AND state<>'cancelled')
             FOR UPDATE SKIP LOCKED",
        )
        .bind(KIND).bind(&key).bind(&fence.storage_notice_day).bind(clock)
        .fetch_optional(&mut *txn).await?;
        let Some(operation) = operation else {
            return Ok(());
        };
        let writable: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(REPLAY_MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !writable
            || !selection(&mut txn, instance)
                .await
                .map_err(|_| JobStoreError::Database)?
            || !telegram_primary(&mut txn, instance).await?
            || confirmed_pairing(&mut txn, instance, chat).await.ok() != Some(owner)
        {
            return Ok(());
        }
        let conversation = telegram_conversation_id(owner, instance, chat);
        sqlx::query("INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
                     VALUES($1,$2,'telegram',$3,gen_random_uuid(),'{}',false) ON CONFLICT(id) DO NOTHING")
            .bind(conversation).bind(owner).bind(instance).execute(&mut *txn).await?;
        let expires = clock
            .date()
            .next_day()
            .and_then(|day| {
                StoredInstant::try_from(day.with_time(Time::MIDNIGHT).assume_utc()).ok()
            })
            .ok_or(JobStoreError::InvalidRecordedState)?;
        let intent = OutboundIntent::normalize(json!({
            "owner_id":owner.to_string(),"conversation_id":conversation.to_string(),
            "channel_instance":{"channel":"telegram","instance_id":instance},
            "blocks":[{"text":NOTICE}],"expires_at":expires.to_string(),
        }))
        .map_err(|_| JobStoreError::InvalidRecordedState)?;
        let queued = queue_outbox_intent(
            &mut txn,
            QueueIntent {
                intent,
                operation_id: operation,
                inbound_id: None,
                dedup_key: Some(&key),
                requirements: &[],
                now,
            },
        )
        .await
        .map_err(|_| JobStoreError::Database)?;
        sqlx::query("UPDATE outbox_messages SET memory_fence=$2::jsonb,state='queued',
                        next_attempt_at=NULL,lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp()
                     WHERE id=$1 AND (memory_fence IS NULL OR state='cancelled')")
            .bind(queued.outbox_id).bind(json!({"storage_notice_day":fence.storage_notice_day}).to_string())
            .execute(&mut *txn).await?;
        sqlx::query(
            "UPDATE jobs SET status='pending',lease_token=NULL,lease_until=NULL
                     WHERE id=$1 AND status='running'",
        )
        .bind(operation)
        .execute(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(())
    }
}
