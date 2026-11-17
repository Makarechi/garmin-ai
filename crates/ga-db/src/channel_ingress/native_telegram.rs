//! A processing job records native ownership of an accepted Telegram update.

use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::jobs::JobStoreError;

use super::IngressError;

pub(crate) const KIND: &str = "telegram_native_inbound";

/// Python may have accepted an update without creating a neutral shadow row.
/// Match its two possible storage keys and the stored channel namespace.
pub(super) async fn legacy_owns(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
    update_id: i64,
) -> Result<bool, IngressError> {
    let key = Sha256::digest(format!("telegram:{instance}:{update_id}"));
    let mut prefix = [0; 8];
    prefix.copy_from_slice(&key[..8]);
    let alternate_id = -(i64::from_be_bytes(prefix) & i64::MAX).max(1);
    let instance_json = json!({"channel":"telegram","instance_id":instance}).to_string();
    let exists = sqlx::query_scalar(
        "SELECT EXISTS(
           SELECT 1 FROM telegram_updates
           WHERE id IN ($1,$2)
             AND COALESCE(payload->'_channel_instance',
                          '{\"channel\":\"telegram\",\"instance_id\":\"primary\"}'::jsonb)=$3::jsonb
             AND (id=$1 OR payload->>'update_id'=$1::bigint::text)
         )",
    )
    .bind(update_id)
    .bind(alternate_id)
    .bind(instance_json)
    .fetch_one(&mut **txn)
    .await?;
    Ok(exists)
}

pub(super) async fn enqueue(
    txn: &mut Transaction<'_, Postgres>,
    message_id: Uuid,
    epoch: i64,
    received_at: StoredInstant,
) -> Result<(), JobStoreError> {
    let dedup_key = format!("telegram:inbound:native:{message_id}");
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES($1,$2,$3::jsonb,$4,'pending',$5,0)",
    )
    .bind(Uuid::new_v4())
    .bind(KIND)
    .bind(json!({"inbound_message_id":message_id.to_string(),"ordering_epoch":epoch}).to_string())
    .bind(dedup_key)
    .bind(OffsetDateTime::from(received_at))
    .execute(&mut **txn)
    .await?;
    Ok(())
}
