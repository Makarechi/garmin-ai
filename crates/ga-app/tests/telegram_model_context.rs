use std::{cell::Cell, env, error::Error, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    conversation_memory::{ConversationBinding, ConversationReadContext, MemoryError},
    share_policy::{DestinationKind, ModelChannelSharing, ShareDestination},
};
use ga_domain::{
    channel::InboundEnvelope, model_consent::ModelCategories, timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

#[tokio::test]
#[ignore = "checks the authenticated model send fence on a disposable TimescaleDB"]
async fn sends_only_while_the_authenticated_conversation_context_is_current()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner_id).execute(&pool).await?;
    let binding = TelegramBinding {
        owner_id,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(at)?;
    let mut message = json!({
        "message_id": 761, "from": {"id":42}, "chat":{"id":42,"type":"private"},
        "date":at.unix_timestamp(), "text":"Synthetic owner question"
    });
    let original = binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":301,"message":message}),
            now,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing original update")?;
    let raw: String = sqlx::query_scalar("SELECT envelope::text FROM inbound_messages WHERE id=$1")
        .bind(original.message_id)
        .fetch_one(&pool)
        .await?;
    let envelope = InboundEnvelope::parse(raw.as_bytes())?;
    let conversation = ConversationBinding {
        owner_id,
        conversation_id: envelope.conversation_id(),
    };
    let memory = database.conversation_memory(conversation, now).await?;
    let current = ConversationReadContext {
        binding: conversation,
        inbound_id: envelope.message_id(),
        operation_id: original.operation_id,
        memory: &memory.fence,
    };

    let model = ShareDestination::new(DestinationKind::Model, "model:gemini:primary")?;
    let channel = ShareDestination::new(DestinationKind::Channel, "telegram:primary")?;
    let wrong_channel = ShareDestination::new(DestinationKind::Channel, "telegram:other")?;
    let send_count = Cell::new(0);
    let send = || async {
        send_count.set(send_count.get() + 1);
        assert!(matches!(
            database
                .forget_telegram_analysis_context(conversation)
                .await,
            Err(MemoryError::Busy)
        ));
        assert!(matches!(
            database
                .set_conversation_memory_sharing(conversation, true)
                .await,
            Err(MemoryError::Busy)
        ));
        let mut writer = pool.begin().await?;
        let ordinary_write_allowed: bool =
            sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(72104619)")
                .fetch_one(&mut *writer)
                .await?;
        assert!(ordinary_write_allowed);
        writer.rollback().await
    };
    let attempt = |destination, conversation| {
        database.with_model_delivery(
            model,
            ModelCategories::HEALTH_AND_DIARY,
            &[],
            Some(ModelChannelSharing {
                destination,
                requirements: &[],
                conversation: Some(conversation),
            }),
            send,
        )
    };
    assert!(attempt(wrong_channel, current).await?.is_none());
    attempt(channel, current)
        .await?
        .ok_or("Current context was refused")??;
    assert_eq!(send_count.get(), 1);
    database
        .forget_telegram_analysis_context(conversation)
        .await?;
    assert!(attempt(channel, current).await?.is_none());

    let latest = database.conversation_memory(conversation, now).await?;
    let current = ConversationReadContext {
        memory: &latest.fence,
        ..current
    };
    sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE owner_id=$1")
        .bind(owner_id)
        .execute(&pool)
        .await?;
    assert!(attempt(channel, current).await?.is_none());
    sqlx::query("UPDATE channel_bindings SET external_id='42' WHERE owner_id=$1")
        .bind(owner_id)
        .execute(&pool)
        .await?;

    message["text"] = json!("Corrected synthetic question");
    message["edit_date"] = json!(at.unix_timestamp() + 1);
    binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":302,"edited_message":message}),
            now,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing authentic edit")?;
    assert!(attempt(channel, current).await?.is_none());
    assert_eq!(send_count.get(), 1);
    Ok(())
}
