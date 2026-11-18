use ga_domain::{
    channel::{OutboundIntent, telegram_conversation_id},
    timestamp::StoredInstant,
};
use serde::{Deserialize, Serialize};
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
    scenario_packs::source_instance_selected,
    telegram_binding::confirmed_pairing,
};

const STORAGE_KIND: &str = "telegram_storage_notice";
const GARMIN_KIND: &str = "telegram_connection_notice";
const GARMIN_AUTH_NOTICE: &str = "Garmin требует повторного входа. История и дневник доступны. Остановите garmin-ai-rust worker, выполните garmin-ai-rust login и запустите worker тем же способом.";
const GARMIN_ACCOUNT_NOTICE: &str = "Синхронизация Garmin остановлена: владелец аккаунта не подтверждён или не совпадает с владельцем базы. История и дневник доступны. Проверьте исходный аккаунт; для другого владельца нужен отдельный экземпляр. Для старой базы без привязки используйте локальный garmin-ai-rust enroll-account --confirm-existing-owner.";
const NOTICE: &str = "Мало свободного места для следующей резервной копии. Проверьте хранилище и запустите backup-space. Последняя копия не удалялась автоматически.";

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct StorageNotice {
    storage_notice_day: String,
}

#[derive(Clone, Copy, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
enum GarminNoticeCategory {
    Auth,
    AccountBinding,
}

impl GarminNoticeCategory {
    fn name(self) -> &'static str {
        match self {
            Self::Auth => "auth",
            Self::AccountBinding => "account-binding",
        }
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct GarminNotice {
    garmin_notice_day: String,
    garmin_notice_category: GarminNoticeCategory,
    garmin_source: String,
}

#[derive(Deserialize, Serialize)]
#[serde(untagged)]
pub(crate) enum TelegramNoticeFence {
    Storage(StorageNotice),
    Garmin(GarminNotice),
}

impl TelegramNoticeFence {
    fn storage(day: String) -> Self {
        Self::Storage(StorageNotice {
            storage_notice_day: day,
        })
    }

    pub(crate) fn garmin_login_source(&self) -> Option<&str> {
        match self {
            Self::Garmin(notice) if notice.garmin_notice_category == GarminNoticeCategory::Auth => {
                Some(&notice.garmin_source)
            }
            _ => None,
        }
    }

    fn kind(&self) -> &'static str {
        match self {
            Self::Storage(_) => STORAGE_KIND,
            Self::Garmin(_) => GARMIN_KIND,
        }
    }

    fn day(&self) -> &str {
        match self {
            Self::Storage(notice) => &notice.storage_notice_day,
            Self::Garmin(notice) => &notice.garmin_notice_day,
        }
    }

    fn key(&self) -> String {
        let prefix = match self {
            Self::Storage(_) => "storage-notice",
            Self::Garmin(notice) => notice.garmin_notice_category.name(),
        };
        format!("{prefix}:{}", self.day())
    }

    fn payload(&self) -> Value {
        match self {
            Self::Storage(notice) => json!({"day":notice.storage_notice_day}),
            Self::Garmin(notice) => {
                json!({"category":notice.garmin_notice_category,"key":self.key()})
            }
        }
    }

    fn text(&self) -> &'static str {
        match self {
            Self::Storage(_) => NOTICE,
            Self::Garmin(notice) => match notice.garmin_notice_category {
                GarminNoticeCategory::Auth => GARMIN_AUTH_NOTICE,
                GarminNoticeCategory::AccountBinding => GARMIN_ACCOUNT_NOTICE,
            },
        }
    }

    fn receipt(&self) -> String {
        format!("outbox:{}:0", self.key())
    }

    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        now: StoredInstant,
    ) -> Result<bool, sqlx::Error> {
        if self.day() != OffsetDateTime::from(now).date().to_string() {
            return Ok(false);
        }
        let active = match self {
            Self::Storage(_) => {
                sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='storage:backup-capacity'
                                           AND value->>'status'='insufficient')",
                )
                .fetch_one(&mut **txn)
                .await?
            }
            Self::Garmin(notice) => {
                garmin_category(txn, &notice.garmin_source).await?
                    == Some(notice.garmin_notice_category)
            }
        };
        if !active {
            return Ok(false);
        }
        let receipt: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                .bind(self.receipt())
                .fetch_optional(&mut **txn)
                .await?;
        Ok(receipt.as_deref().is_none_or(|raw| {
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

    /// An explicit, authorized retry must also be safe if Python resumes the queue.
    pub(crate) async fn reopen(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        instance: &str,
    ) -> Result<bool, sqlx::Error> {
        let key = self.key();
        let (blocked, at): (bool, OffsetDateTime) = sqlx::query_as(
            "SELECT EXISTS(SELECT 1 FROM app_state WHERE key=$1 AND value->>'status'='sent')
                 OR EXISTS(SELECT 1 FROM jobs WHERE dedup_key=$2 AND kind=$3
                   AND status='running' AND lease_until>clock_timestamp()),
                   clock_timestamp()",
        )
        .bind(self.receipt())
        .bind(&key)
        .bind(self.kind())
        .fetch_one(&mut **txn)
        .await?;
        if blocked {
            return Ok(false);
        }
        let when = StoredInstant::try_from(at)
            .map_err(|_| sqlx::Error::Protocol("Invalid Telegram notice clock".into()))?;
        self.retry(txn, instance, when).await?;
        sqlx::query(
            "UPDATE jobs SET status='pending',run_at=$2,completed_at=NULL,last_error=NULL,
                        attempts=0,lease_token=NULL,lease_until=NULL
                     WHERE dedup_key=$1 AND kind=$3",
        )
        .bind(&key)
        .bind(at)
        .bind(self.kind())
        .execute(&mut **txn)
        .await?;
        Ok(true)
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
        .bind(self.kind())
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

async fn garmin_category(
    txn: &mut Transaction<'_, Postgres>,
    source: &str,
) -> Result<Option<GarminNoticeCategory>, sqlx::Error> {
    if !source_instance_selected(txn, source).await? {
        return Ok(None);
    }
    let reason: Option<String> = sqlx::query_scalar(
        "SELECT coalesce(value->>'reason_class','') FROM app_state
         WHERE key='integration:garmin' AND value->>'status'='reauth_required'",
    )
    .fetch_optional(&mut **txn)
    .await?;
    Ok(reason.map(|reason| {
        if reason.starts_with("Account") {
            GarminNoticeCategory::AccountBinding
        } else {
            GarminNoticeCategory::Auth
        }
    }))
}

pub(super) async fn schedule(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
) -> Result<(), sqlx::Error> {
    let fence = TelegramNoticeFence::storage(OffsetDateTime::from(now).date().to_string());
    schedule_notice(txn, &fence, now).await
}

async fn schedule_notice(
    txn: &mut Transaction<'_, Postgres>,
    fence: &TelegramNoticeFence,
    now: StoredInstant,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
                 VALUES($1,$2,$3::jsonb,$4,'pending',$5,0)
                 ON CONFLICT(dedup_key) DO UPDATE SET status='pending',run_at=excluded.run_at,
                    attempts=0,completed_at=NULL,last_error=NULL,lease_until=NULL,lease_token=NULL
                 WHERE jobs.kind=$2 AND jobs.status='done'
                 AND NOT EXISTS(SELECT 1 FROM app_state WHERE key=$6)",
    )
    .bind(Uuid::new_v4())
    .bind(fence.kind())
    .bind(fence.payload().to_string())
    .bind(fence.key())
    .bind(OffsetDateTime::from(now))
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
        let fence = TelegramNoticeFence::storage(OffsetDateTime::from(now).date().to_string());
        let txn = begin(&self.work).await?;
        queue_notice(txn, &fence, owner, instance, chat, now).await
    }

    /// Notify the selected primary Telegram owner about a fresh Garmin pause.
    pub async fn queue_telegram_garmin_notice(
        &self,
        owner: Uuid,
        instance: &str,
        chat: i64,
        source: &str,
        now: StoredInstant,
    ) -> Result<(), JobStoreError> {
        let mut txn = begin(&self.work).await?;
        let Some(category) = garmin_category(&mut txn, source).await? else {
            return Ok(());
        };
        let fence = TelegramNoticeFence::Garmin(GarminNotice {
            garmin_notice_day: OffsetDateTime::from(now).date().to_string(),
            garmin_notice_category: category,
            garmin_source: source.to_owned(),
        });
        if !fence.current(&mut txn, now).await? || !telegram_primary(&mut txn, instance).await? {
            return Ok(());
        }
        schedule_notice(&mut txn, &fence, now).await?;
        queue_notice(txn, &fence, owner, instance, chat, now).await
    }
}

async fn queue_notice(
    mut txn: Transaction<'_, Postgres>,
    fence: &TelegramNoticeFence,
    owner: Uuid,
    instance: &str,
    chat: i64,
    now: StoredInstant,
) -> Result<(), JobStoreError> {
    let clock = OffsetDateTime::from(now);
    let key = fence.key();
    let operation: Option<Uuid> = sqlx::query_scalar(
        "SELECT id FROM jobs WHERE kind=$1 AND dedup_key=$2 AND payload @> $3::jsonb
                AND ((status='pending' AND run_at<=$4)
                  OR (status='running' AND lease_until<$4))
             FOR UPDATE SKIP LOCKED",
    )
    .bind(fence.kind())
    .bind(&key)
    .bind(fence.payload().to_string())
    .bind(clock)
    .fetch_optional(&mut *txn)
    .await?;
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
        || !fence.current(&mut txn, now).await?
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
        .and_then(|day| StoredInstant::try_from(day.with_time(Time::MIDNIGHT).assume_utc()).ok())
        .ok_or(JobStoreError::InvalidRecordedState)?;
    let intent = OutboundIntent::normalize(json!({
        "owner_id":owner.to_string(),"conversation_id":conversation.to_string(),
        "channel_instance":{"channel":"telegram","instance_id":instance},
        "blocks":[{"text":fence.text()}],"expires_at":expires.to_string(),
    }))
    .map_err(|_| JobStoreError::InvalidRecordedState)?;
    // Keep the old history and free the daily key for a definitely unsent notice.
    // Claiming a reply uses the same replay mutation lock.
    sqlx::query(
        "UPDATE outbox_messages o SET dedup_key=concat($1,':cancelled:',o.id),state='cancelled',
                next_attempt_at=NULL,updated_at=clock_timestamp()
         WHERE o.dedup_key=$1 AND o.operation_id=$2
           AND (o.state='cancelled' OR (o.state='queued' AND o.lease_token IS NULL AND o.lease_until IS NULL))
           AND o.inbound_message_id IS NULL AND o.legacy_key IS NULL
           AND (o.owner_id,o.conversation_id)<>($3,$4)
           AND coalesce(o.memory_fence->>'storage_notice_day',o.memory_fence->>'garmin_notice_day')=$5
           AND NOT EXISTS(SELECT 1 FROM message_delivery_receipts r
             WHERE r.outbox_message_id=o.id
               AND r.state IN ('provider_accepted','delivered','read','uncertain'))",
    )
    .bind(&key)
    .bind(operation)
    .bind(owner)
    .bind(conversation)
    .bind(fence.day())
    .execute(&mut *txn)
    .await?;
    let already_pending: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM outbox_messages o WHERE o.dedup_key=$1 AND
         (o.state<>'cancelled' OR o.operation_id IS DISTINCT FROM $2
          OR o.inbound_message_id IS NOT NULL OR o.legacy_key IS NOT NULL
          OR (o.owner_id,o.conversation_id)<>($3,$4)
          OR coalesce(o.memory_fence->>'storage_notice_day',o.memory_fence->>'garmin_notice_day') IS DISTINCT FROM $5
          OR EXISTS(SELECT 1 FROM message_delivery_receipts r WHERE r.outbox_message_id=o.id
                    AND r.state IN ('provider_accepted','delivered','read','uncertain'))))",
    )
    .bind(&key)
    .bind(operation)
    .bind(owner)
    .bind(conversation)
    .bind(fence.day())
    .fetch_one(&mut *txn)
    .await?;
    if already_pending {
        return Ok(());
    }
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
            .bind(queued.outbox_id).bind(serde_json::to_string(fence).map_err(|_| JobStoreError::InvalidRecordedState)?)
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
