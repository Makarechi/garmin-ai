use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{ConversationBinding, DEADLINE, MemoryError, clear_conversation, retire_dependencies};
use crate::Database;

const OWNER_SETTINGS_LOCK: i64 = 72_104_621;
const LEGACY_CONTEXT_LOCK: i64 = 72_104_643;

impl Database {
    /// Clear current neutral Telegram memory and the old owner's Telegram
    /// analysis context, so changing workers cannot restore a forgotten answer.
    pub async fn forget_telegram_analysis_context(
        &self,
        binding: ConversationBinding,
    ) -> Result<Uuid, MemoryError> {
        tokio::time::timeout(DEADLINE, async {
            let mut txn = self.begin_memory().await?;
            let epoch = forget_in(&mut txn, binding).await?;
            txn.commit().await?;
            Ok(epoch)
        })
        .await
        .map_err(|_| MemoryError::Database)?
    }
}

async fn forget_in(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
) -> Result<Uuid, MemoryError> {
    let telegram: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM conversations WHERE id=$1 AND owner_id=$2 AND channel='telegram')",
    )
    .bind(binding.conversation_id)
    .bind(binding.owner_id)
    .fetch_one(&mut **txn)
    .await?;
    if !telegram {
        return Err(MemoryError::NotFound);
    }
    let policy: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
        .bind(OWNER_SETTINGS_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !policy {
        return Err(MemoryError::Busy);
    }
    let context: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(LEGACY_CONTEXT_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !context {
        return Err(MemoryError::Busy);
    }

    let epoch = clear_conversation(txn, binding).await?;
    retire_dependencies(txn, binding).await?;
    sqlx::query(
        "WITH pending AS (
             DELETE FROM app_state WHERE starts_with(key,'analysis:conversation:pending')
             RETURNING value#>>'{turn,update_id}' AS update_id
         ), cached AS (
             SELECT item->>'update_id' AS update_id
             FROM app_state AS memory
             CROSS JOIN LATERAL jsonb_array_elements(
                 CASE WHEN jsonb_typeof(memory.value->'turns')='array'
                      THEN memory.value->'turns' ELSE '[]'::jsonb END
             ) AS item
             WHERE memory.key='analysis:conversation'
         ), retired AS (
             SELECT update_id FROM pending UNION SELECT update_id FROM cached
         )
         UPDATE app_state AS replies
         SET value=replies.value || jsonb_build_object(
             'text','Контекст анализа удалён.',
             'status','forgotten','keyboard',false,'kind','analysis'
         ), updated_at=now()
         WHERE starts_with(replies.key,'telegram:reply:')
           AND (replies.value->>'kind'='analysis' OR replies.key IN (
               SELECT concat('telegram:reply:',update_id) FROM retired WHERE update_id IS NOT NULL
           ))",
    )
    .execute(&mut **txn)
    .await?;
    sqlx::query(
        "INSERT INTO app_state(key,value)
         VALUES('analysis:conversation',jsonb_build_object('epoch',$1::text,'turns','[]'::jsonb))
         ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
    )
    .bind(epoch.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(epoch)
}
