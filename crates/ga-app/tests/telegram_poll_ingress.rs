use std::{env, error::Error, fs, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database, channel_ingress::telegram_poll::TelegramPollError, runtime_lease::RuntimeLeaseError,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "exercises atomic native polling on disposable TimescaleDB with Python-signed buttons"]
async fn poll_commits_inbox_button_and_cursor_under_the_runtime_lease() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut lease = database.acquire_runtime_lease().await?;
    assert!(matches!(
        database.acquire_runtime_lease().await,
        Err(RuntimeLeaseError::Occupied)
    ));
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_CALLBACKS")?)?)?;
    let owner: Uuid = fixture["owner"].as_str().ok_or("Missing owner")?.parse()?;
    let now: StoredInstant = fixture["now"].as_str().ok_or("Missing time")?.parse()?;
    let external_owner_id = fixture["external_owner"].as_i64().ok_or("Missing chat")?;
    let binding = TelegramBinding {
        owner_id: owner,
        external_owner_id,
        instance_id: "primary",
    };
    let secondary = TelegramBinding {
        instance_id: "secondary",
        ..binding
    };
    let cases = &fixture["cases"];
    let message = json!({
        "message_id":7001,"text":"synthetic message 🧪","date":1_793_883_600,
        "chat":{"type":"private","id":external_owner_id},"from":{"id":external_owner_id}
    });

    let first = json!({"update_id":1010,"message":message});
    let receipt = binding
        .accept_polled_update(&mut lease, &first, now, false)
        .await?;
    let original = receipt.inbound.ok_or("Missing first inbox message")?;
    assert!(original.created);
    assert_eq!(receipt.new_ordering_epoch, Some(0));
    assert_eq!(offset(&database, "primary", now).await?, Some(1011));

    let mut edited_message = message.clone();
    edited_message["text"] = json!("synthetic edit");
    edited_message["edit_date"] = json!(1_793_883_660);
    let edit = json!({"update_id":1011,"edited_message":edited_message});
    let receipt = binding
        .accept_polled_update(&mut lease, &edit, now, false)
        .await?;
    assert_eq!(receipt.new_ordering_epoch, None);
    let revised = receipt.inbound.ok_or("Missing edit")?;
    assert_eq!(revised.supersedes_id, Some(original.message_id));
    assert_eq!(revised.operation_id, original.operation_id);
    let repeated = binding
        .accept_polled_update(&mut lease, &edit, now, false)
        .await?;
    assert_eq!(
        repeated.inbound.ok_or("Missing duplicate")?.message_id,
        revised.message_id
    );
    assert_eq!(repeated.new_ordering_epoch, None);
    let mut foreign = json!({"update_id":1012,"message":message});
    foreign["message"]["from"]["id"] = json!(external_owner_id + 1);
    let discarded = binding
        .accept_polled_update(&mut lease, &foreign, now, false)
        .await?;
    assert!(discarded.selected && discarded.inbound.is_none());
    assert_eq!(offset(&database, "primary", now).await?, Some(1013));

    sqlx::query("INSERT INTO app_state(key,value) VALUES('preferences:onboarding',$1::jsonb)")
        .bind(json!({"channel":{"channel":"web","instance_id":"primary"},"fallback_channels":[{"channel":"telegram","instance_id":"primary"}]}).to_string())
        .execute(&pool).await?;
    let skipped = secondary
        .accept_polled_update(&mut lease, &first, now, false)
        .await?;
    assert!(!skipped.selected && skipped.inbound.is_none());
    assert_eq!(offset(&database, "secondary", now).await?, Some(1011));

    let mut policy = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104621)")
        .execute(&mut *policy)
        .await?;
    let valid = &cases["valid"]["update"];
    assert!(matches!(
        binding
            .accept_polled_update(&mut lease, valid, now, true)
            .await,
        Err(TelegramPollError::Busy)
    ));
    assert_eq!(offset(&database, "primary", now).await?, Some(1013));
    policy.rollback().await?;

    let mut with_claimed_time = valid.clone();
    with_claimed_time["_callback_time_known"] = json!(true);
    let callback = binding
        .accept_polled_update(&mut lease, &with_claimed_time, now, false)
        .await?;
    let action = callback.inbound.ok_or("Missing accepted action")?;
    assert!(action.created && !stored_token(&pool, &cases["valid"]).await?);
    assert_eq!(offset(&database, "primary", now).await?, Some(9002));
    let precision: String =
        sqlx::query_scalar("SELECT envelope->>'time_precision' FROM inbound_messages WHERE id=$1")
            .bind(action.message_id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(precision, "unknown");
    let repeated = binding
        .accept_polled_update(&mut lease, valid, now, true)
        .await?;
    assert_eq!(
        repeated
            .inbound
            .ok_or("Missing repeated action")?
            .message_id,
        action.message_id
    );
    assert_eq!(repeated.new_ordering_epoch, None);

    sqlx::query("ALTER TABLE app_state ADD CONSTRAINT synthetic_cursor_failure CHECK(key <> 'telegram:offset' OR value->>'offset' <> '9003')")
        .execute(&pool).await?;
    let roll = &cases["rollback"]["update"];
    assert!(matches!(
        binding
            .accept_polled_update(&mut lease, roll, now, true)
            .await,
        Err(TelegramPollError::Cursor(_))
    ));
    assert!(stored_token(&pool, &cases["rollback"]).await?);
    assert_eq!(offset(&database, "primary", now).await?, Some(9002));
    let incomplete: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM inbound_messages WHERE external_event_id='9002')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(!incomplete);
    sqlx::query("ALTER TABLE app_state DROP CONSTRAINT synthetic_cursor_failure")
        .execute(&pool)
        .await?;
    assert!(
        binding
            .accept_polled_update(&mut lease, roll, now, true)
            .await?
            .inbound
            .is_some()
    );
    assert!(!stored_token(&pool, &cases["rollback"]).await?);
    assert_eq!(offset(&database, "primary", now).await?, Some(9003));

    let rejected = binding
        .accept_polled_update(&mut lease, &cases["stale"]["update"], now, true)
        .await?;
    assert!(rejected.selected && rejected.inbound.is_none());
    assert!(stored_token(&pool, &cases["stale"]).await?);
    assert_eq!(offset(&database, "primary", now).await?, Some(9004));
    let legacy_rows: i64 = sqlx::query_scalar("SELECT count(*) FROM telegram_updates")
        .fetch_one(&pool)
        .await?;
    assert_eq!(legacy_rows, 0);
    println!(
        "Verified atomic native polling with Python-signed buttons, edits, repeated events, channel selection, and failed cursor commit"
    );
    Ok(())
}

async fn offset(
    database: &Database,
    instance: &str,
    now: StoredInstant,
) -> Result<Option<i64>, Box<dyn Error>> {
    Ok(database.telegram_poll_cursor(instance, now).await?.offset)
}

async fn stored_token(pool: &PgPool, case: &Value) -> Result<bool, Box<dyn Error>> {
    let id: Uuid = case["outbox_id"]
        .as_str()
        .ok_or("Missing outbox message")?
        .parse()?;
    Ok(sqlx::query_scalar(
        "SELECT (intent->'actions'->0->>'token') IS NOT NULL FROM outbox_messages WHERE id=$1",
    )
    .bind(id)
    .fetch_one(pool)
    .await?)
}
