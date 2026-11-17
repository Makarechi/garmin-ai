use std::{env, error::Error, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    jobs::{
        JobStoreError,
        telegram_inbound::{TelegramControlError, TelegramInboundTarget},
    },
};
use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

#[tokio::test]
#[ignore = "runs authenticated Telegram controls in a disposable TimescaleDB"]
async fn applies_controls_with_their_durable_reply() -> Result<(), Box<dyn Error>> {
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
    let database_name: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(expected, database_name);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    let binding = TelegramBinding {
        owner_id: owner,
        instance_id: "primary",
        external_owner_id: 42,
    };
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let clock = OffsetDateTime::now_utc();
    let now = StoredInstant::try_from(clock.replace_nanosecond(clock.microsecond() * 1000)?)?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let wire = |update, message: &str| {
        json!({
            "update_id":update,
            "message":{"message_id":update,"date":clock.unix_timestamp(),
                "from":{"id":42},"chat":{"id":42,"type":"private"},"text":message},
        })
    };
    for (update, message) in [
        (1, "Synthetic diary message"),
        (20, "/forget_conversation"),
        (21, "/pause"),
        (22, "/resume"),
        (23, "/today"),
    ] {
        let written = binding
            .accept_polled_update(&mut runtime, &wire(update, message), now, false)
            .await?;
        assert!(written.inbound.is_some_and(|row| row.created));
    }
    let diary = database
        .claim_telegram_inbound(target, now, 60)
        .await?
        .ok_or("Missing diary lease")?;

    let (memory,): (Uuid,) = sqlx::query_as(
        "UPDATE conversations SET state='{\"analysis_turns\":[{\"question\":\"synthetic\"}]}'
         WHERE owner_id=$1 AND channel='telegram' RETURNING memory_epoch",
    )
    .bind(owner)
    .fetch_one(&pool)
    .await?;
    sqlx::raw_sql(
        "INSERT INTO app_state(key,value) VALUES
           ('analysis:conversation','{\"turns\":[{\"update_id\":981}]}'),
           ('telegram:reply:981','{\"text\":\"synthetic analysis\",\"kind\":\"analysis\"}');
         CREATE FUNCTION fixture_fail_owner_reply() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN RAISE EXCEPTION 'simulated owner reply failure'; END; $$;
         CREATE TRIGGER fixture_fail_owner_reply BEFORE INSERT ON outbox_messages
         FOR EACH ROW EXECUTE FUNCTION fixture_fail_owner_reply();",
    )
    .execute(&pool)
    .await?;

    let forget = database
        .claim_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing forget lease")?;
    assert_ne!(forget.message_id(), diary.message_id());
    assert!(matches!(
        database.apply_telegram_owner_control(&forget, 3).await,
        Err(TelegramControlError::Outbox(_))
    ));
    let (epoch, count): (Uuid, i64) = sqlx::query_as(
        "SELECT memory_epoch,(SELECT count(*) FROM outbox_messages)
         FROM conversations WHERE owner_id=$1 AND channel='telegram'",
    )
    .bind(owner)
    .fetch_one(&pool)
    .await?;
    assert_eq!((epoch, count), (memory, 0));
    let old_reply: String =
        sqlx::query_scalar("SELECT value->>'text' FROM app_state WHERE key='telegram:reply:981'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(old_reply, "synthetic analysis");
    sqlx::query("DROP TRIGGER fixture_fail_owner_reply ON outbox_messages")
        .execute(&pool)
        .await?;
    let applied = database.apply_telegram_owner_control(&forget, 3).await?;
    assert!(applied.created);
    assert!(matches!(
        database.apply_telegram_owner_control(&forget, 3).await,
        Err(TelegramControlError::Job(JobStoreError::LeaseLost))
    ));
    let committed: (Uuid, Option<String>) = sqlx::query_as(
        "SELECT memory_epoch,state->'analysis_turns'->>0 FROM conversations
         WHERE owner_id=$1 AND channel='telegram'",
    )
    .bind(owner)
    .fetch_optional(&pool)
    .await?
    .ok_or("Missing conversation")?;
    assert_ne!(committed.0, memory);
    assert!(committed.1.is_none());
    let retired: String =
        sqlx::query_scalar("SELECT value->>'status' FROM app_state WHERE key='telegram:reply:981'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(retired, "forgotten");
    let duplicate = binding
        .accept_polled_update(&mut runtime, &wire(20, "/forget_conversation"), now, false)
        .await?;
    assert!(duplicate.inbound.is_some_and(|row| !row.created));

    let pause = database
        .claim_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing pause lease")?;
    sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE owner_id=$1")
        .bind(owner)
        .execute(&pool)
        .await?;
    assert!(matches!(
        database.apply_telegram_owner_control(&pause, 3).await,
        Err(TelegramControlError::Pairing(_) | TelegramControlError::BindingChanged)
    ));
    assert!(
        sqlx::query_scalar::<_, String>(
            "SELECT value::text FROM app_state WHERE key='proactive:enabled'"
        )
        .fetch_optional(&pool)
        .await?
        .is_none()
    );
    sqlx::query("UPDATE channel_bindings SET external_id='42' WHERE owner_id=$1")
        .bind(owner)
        .execute(&pool)
        .await?;
    assert!(
        database
            .apply_telegram_owner_control(&pause, 3)
            .await?
            .created
    );
    let resume = database
        .claim_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing resume lease")?;
    assert!(
        database
            .apply_telegram_owner_control(&resume, 4)
            .await?
            .created
    );
    let today = database
        .claim_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing status lease")?;
    assert!(matches!(
        database.apply_telegram_owner_control(&today, 4).await,
        Err(TelegramControlError::Unsupported)
    ));
    let state: String =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='proactive:enabled'")
            .fetch_one(&pool)
            .await?;
    let state: serde_json::Value = serde_json::from_str(&state)?;
    assert_eq!(state["enabled"], true);
    assert_eq!(state["update_id"], 22);
    let replies: Vec<(String, String, String, Uuid)> = sqlx::query_as(
        "SELECT i.external_event_id,o.intent#>>'{blocks,0,text}',i.status,o.operation_id
         FROM outbox_messages o JOIN inbound_messages i ON i.id=o.inbound_message_id
         ORDER BY i.external_event_id::bigint",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        replies,
        [
            (
                "20".into(),
                "Контекст аналитического разговора очищен. Записи дневника сохранены.".into(),
                "processed".into(),
                forget.operation_id()
            ),
            (
                "21".into(),
                "Вопросы отключены. Синхронизация продолжается.".into(),
                "processed".into(),
                pause.operation_id()
            ),
            (
                "22".into(),
                "Настройка сохранена: вопросы разрешены. Лимит в день: 4, только вне тихих часов."
                    .into(),
                "processed".into(),
                resume.operation_id()
            ),
        ]
    );
    assert!(
        database
            .claim_telegram_inbound(target, now, 60)
            .await?
            .is_none()
    );
    println!(
        "Verified three durable owner controls, retry rollback, stale pairing and replay protection"
    );
    Ok(())
}
