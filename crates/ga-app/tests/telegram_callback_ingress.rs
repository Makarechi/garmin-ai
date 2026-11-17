use std::{env, error::Error, fs, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{Database, channel_ingress::IngressError};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "accepts Python-signed buttons in a disposable TimescaleDB"]
async fn callback_consumption_and_ingress_commit_together() -> Result<(), Box<dyn Error>> {
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
    assert_eq!(target, actual);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_CALLBACKS")?)?)?;
    let owner: Uuid = fixture["owner"]
        .as_str()
        .ok_or("Missing fixture owner")?
        .parse()?;
    let now: StoredInstant = fixture["now"]
        .as_str()
        .ok_or("Missing fixture time")?
        .parse()?;
    let cases = &fixture["cases"];
    let binding = TelegramBinding {
        external_owner_id: fixture["external_owner"]
            .as_i64()
            .ok_or("Missing private chat")?,
        owner_id: owner,
        instance_id: "primary",
    };
    let original = &cases["valid"]["update"];
    let foreign = TelegramBinding {
        instance_id: "secondary",
        ..binding
    };
    assert!(matches!(
        foreign
            .authenticate(original)?
            .persist_callback(&database, now, false)
            .await,
        Err(IngressError::ActionUnavailable)
    ));

    let update = binding.authenticate(original)?;
    let first = update.persist_callback(&database, now, false).await?;
    assert!(first.created);
    assert_eq!(
        Some(first.operation_id.to_string().as_str()),
        cases["valid"]["operation"].as_str()
    );
    let duplicate = update.persist_callback(&database, now, false).await?;
    assert!(!duplicate.created);
    assert_eq!(first.message_id, duplicate.message_id);
    assert_eq!(first.operation_id, duplicate.operation_id);
    let mut another_event = original.clone();
    another_event["update_id"] = json!(9010);
    assert!(matches!(
        binding
            .authenticate(&another_event)?
            .persist_callback(&database, now, false)
            .await,
        Err(IngressError::ActionUnavailable)
    ));
    let saved = envelope(&pool, first.message_id).await?;
    assert_eq!(saved["action"]["action_id"], cases["valid"]["action_id"]);
    assert!(saved["action"]["token"].is_null());
    assert_eq!(saved["conversation_id"], fixture["conversation"]);
    assert_eq!(saved["time_precision"], "unknown");

    let stale = binding.authenticate(&cases["stale"]["update"])?;
    assert!(matches!(
        stale.persist_callback(&database, now, false).await,
        Err(IngressError::ActionUnavailable)
    ));
    let expired = binding.authenticate(&cases["expired"]["update"])?;
    let deadline = StoredInstant::from_unix_microseconds(now.unix_microseconds() + 1_000_000)?;
    assert!(matches!(
        expired.persist_callback(&database, deadline, true).await,
        Err(IngressError::ActionUnavailable)
    ));

    let rollback = binding.authenticate(&cases["rollback"]["update"])?;
    sqlx::query("ALTER TABLE inbound_messages ADD CONSTRAINT synthetic_callback_failure CHECK(external_event_id<>'9002')")
        .execute(&pool).await?;
    assert!(matches!(
        rollback.persist_callback(&database, now, true).await,
        Err(IngressError::Database)
    ));
    assert!(stored_outbox_token(&pool, &cases["rollback"]).await?);
    sqlx::query("ALTER TABLE inbound_messages DROP CONSTRAINT synthetic_callback_failure")
        .execute(&pool)
        .await?;
    let accepted = rollback.persist_callback(&database, now, true).await?;
    assert!(accepted.created);
    assert!(!stored_outbox_token(&pool, &cases["rollback"]).await?);
    assert!(!stored_outbox_token(&pool, &cases["valid"]).await?);
    assert!(stored_outbox_token(&pool, &cases["stale"]).await?);
    assert!(stored_outbox_token(&pool, &cases["expired"]).await?);
    let saved = envelope(&pool, accepted.message_id).await?;
    assert_eq!(saved["action"]["action_id"], cases["rollback"]["action_id"]);
    assert_eq!(saved["occurred_at"], "2099-07-02T10:00:00Z");
    assert_eq!(saved["time_precision"], "second");
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM inbound_messages WHERE kind='action'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(count, 2);
    println!(
        "Verified Python-signed callbacks, duplicate and foreign events, expiry, stale revision and atomic rollback"
    );
    Ok(())
}

async fn envelope(pool: &PgPool, id: Uuid) -> Result<Value, Box<dyn Error>> {
    let raw: String = sqlx::query_scalar("SELECT envelope::text FROM inbound_messages WHERE id=$1 AND legacy_telegram_update_id IS NULL")
        .bind(id).fetch_one(pool).await?;
    Ok(serde_json::from_str(&raw)?)
}

async fn stored_outbox_token(pool: &PgPool, case: &Value) -> Result<bool, Box<dyn Error>> {
    let id: Uuid = case["outbox_id"]
        .as_str()
        .ok_or("Missing outbox ID")?
        .parse()?;
    let present = sqlx::query_scalar(
        "SELECT (intent->'actions'->0->>'token') IS NOT NULL FROM outbox_messages WHERE id=$1",
    )
    .bind(id)
    .fetch_one(pool)
    .await?;
    Ok(present)
}
