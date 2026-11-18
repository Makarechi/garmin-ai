use ga_domain::channel::telegram_conversation_id;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::SchemaMigrationError;

/// Recover the old singleton conversation and rebuild voice attachment metadata.
pub(super) async fn repair_legacy_backfill(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), SchemaMigrationError> {
    let owners: Vec<(Uuid, Uuid, String, String)> = sqlx::query_as(
        "SELECT owner.id,old.id,old.channel_instance_id,COALESCE(
            (SELECT external_id FROM public.channel_bindings
             WHERE owner_id=owner.id AND channel='telegram' ORDER BY confirmed_at LIMIT 1),
            (SELECT COALESCE(payload #>> '{message,chat,id}',
                             payload #>> '{callback_query,message,chat,id}')
             FROM public.telegram_updates ORDER BY received_at LIMIT 1),
            'legacy-owner')
         FROM public.people owner JOIN public.conversations old
         ON old.id=md5('legacy:telegram:conversation:' || owner.id::text)::uuid
           AND old.owner_id=owner.id AND old.channel='telegram'",
    )
    .fetch_all(&mut **txn)
    .await?;
    for (owner, before, instance, external) in owners {
        let after = telegram_conversation_id(owner, "primary", &external);
        if before == after {
            continue;
        }
        sqlx::query(
            "INSERT INTO public.conversations(id,owner_id,channel,channel_instance_id,external_conversation_id,
                memory_epoch,state,share_owner_memory,created_at,updated_at)
             SELECT $2,owner_id,channel,channel_instance_id,$3,
                memory_epoch,state,share_owner_memory,created_at,updated_at
             FROM public.conversations WHERE id=$1 ON CONFLICT DO NOTHING",
        ).bind(before).bind(after).bind(external).execute(&mut **txn).await?;
        verify_target(txn, after, owner, &instance).await?;
        move_history(txn, before, after).await?;
    }
    sqlx::raw_sql(include_str!("telegram_voice_backfill.sql"))
        .execute(&mut **txn)
        .await?;
    Ok(())
}

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
    verify_target(txn, after, owner, instance).await?;
    move_history(txn, before, after).await?;
    sqlx::query("UPDATE public.conversations SET external_conversation_id=$2 WHERE id=$1")
        .bind(after)
        .bind(external)
        .execute(&mut **txn)
        .await?;
    Ok(())
}

async fn verify_target(
    txn: &mut Transaction<'_, Postgres>,
    target: Uuid,
    owner: Uuid,
    instance: &str,
) -> Result<(), SchemaMigrationError> {
    let matches: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT FROM public.conversations WHERE id=$1 AND owner_id=$2
         AND channel='telegram' AND channel_instance_id=$3)",
    )
    .bind(target)
    .bind(owner)
    .bind(instance)
    .fetch_one(&mut **txn)
    .await?;
    if !matches {
        return Err(SchemaMigrationError::ConversationCollision);
    }
    Ok(())
}

async fn move_history(
    txn: &mut Transaction<'_, Postgres>,
    before: Uuid,
    after: Uuid,
) -> Result<(), SchemaMigrationError> {
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
    Ok(())
}
