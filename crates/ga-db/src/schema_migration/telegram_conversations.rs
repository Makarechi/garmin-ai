use ga_domain::channel::telegram_conversation_id;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::SchemaMigrationError;

/// Match Alembic's original, ordered selection even when a target row already exists.
pub(super) async fn repair(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), SchemaMigrationError> {
    sqlx::query(
        "DECLARE garmin_telegram_repair NO SCROLL CURSOR FOR
         SELECT id,owner_id,channel_instance_id,external_conversation_id FROM public.conversations
         WHERE channel='telegram' AND external_conversation_id IS NOT NULL ORDER BY created_at,id",
    )
    .execute(&mut **txn)
    .await?;
    loop {
        let rows: Vec<(Uuid, Uuid, String, String)> =
            sqlx::query_as("FETCH FORWARD 128 FROM garmin_telegram_repair")
                .fetch_all(&mut **txn)
                .await?;
        if rows.is_empty() {
            break;
        }
        for (before, owner, instance, external) in rows {
            let after = telegram_conversation_id(owner, &instance, &external);
            if before != after {
                move_conversation(txn, before, after, owner, &instance, &external).await?;
            }
        }
    }
    sqlx::query("CLOSE garmin_telegram_repair")
        .execute(&mut **txn)
        .await?;
    Ok(())
}

async fn move_conversation(
    txn: &mut Transaction<'_, Postgres>,
    before: Uuid,
    after: Uuid,
    owner: Uuid,
    instance: &str,
    external: &str,
) -> Result<(), SchemaMigrationError> {
    sqlx::query(
        "INSERT INTO public.conversations(id,owner_id,channel,channel_instance_id,external_conversation_id,
            memory_epoch,state,share_owner_memory,created_at,updated_at)
         SELECT $2,owner_id,channel,channel_instance_id,NULL,
            memory_epoch,state,share_owner_memory,created_at,updated_at
         FROM public.conversations WHERE id=$1 ON CONFLICT (id) DO NOTHING",
    ).bind(before).bind(after).execute(&mut **txn).await?;
    let matches: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT FROM public.conversations WHERE id=$1 AND owner_id=$2
         AND channel='telegram' AND channel_instance_id=$3)",
    )
    .bind(after)
    .bind(owner)
    .bind(instance)
    .fetch_one(&mut **txn)
    .await?;
    if !matches {
        return Err(SchemaMigrationError::ConversationCollision);
    }
    for statement in [
        "UPDATE public.inbound_messages SET conversation_id=$2 WHERE conversation_id=$1",
        "UPDATE public.outbox_messages SET conversation_id=$2 WHERE conversation_id=$1",
    ] {
        sqlx::query(statement)
            .bind(before)
            .bind(after)
            .execute(&mut **txn)
            .await?;
    }
    sqlx::query("DELETE FROM public.conversations WHERE id=$1")
        .bind(before)
        .execute(&mut **txn)
        .await?;
    sqlx::query("UPDATE public.conversations SET external_conversation_id=$2 WHERE id=$1")
        .bind(after)
        .bind(external)
        .execute(&mut **txn)
        .await?;
    Ok(())
}
