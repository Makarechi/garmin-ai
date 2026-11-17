use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    conversation_memory::{ConversationBinding, MemoryError},
};
use serde_json::{Map, Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "compares neutral and legacy memory in disposable TimescaleDB"]
async fn clears_legacy_answers_and_neutral_dependencies_together() -> Result<(), Box<dyn Error>> {
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_FORGET_LEGACY")?)?)?;
    let before = fixture["before"]
        .as_object()
        .ok_or("Missing initial legacy state")?;
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
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    for (key, value) in before {
        sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
            .bind(key)
            .bind(value.to_string())
            .execute(&pool)
            .await?;
    }

    let telegram = Uuid::from_u128(151);
    let other = Uuid::from_u128(152);
    let original_epoch = Uuid::from_u128(153);
    let related = json!({
        "question":"Synthetic shared dependent question",
        "source_epochs": {telegram.to_string():original_epoch.to_string()},
    });
    let independent = json!({"question":"Synthetic independently retained question"});
    for (id, channel, state) in [
        (
            telegram,
            "telegram",
            json!({"analysis_turns":[{"question":"Synthetic current conversation question"}]}),
        ),
        (
            other,
            "web",
            json!({"analysis_turns":[related,independent]}),
        ),
    ] {
        sqlx::query("INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
                    VALUES($1,$2,$3,'primary',$4,$5::jsonb,true)")
            .bind(id).bind(owner).bind(channel).bind(original_epoch).bind(state.to_string())
            .execute(&pool).await?;
    }
    let binding = ConversationBinding {
        owner_id: owner,
        conversation_id: telegram,
    };
    assert!(matches!(
        database
            .forget_telegram_analysis_context(ConversationBinding {
                conversation_id: other,
                ..binding
            })
            .await,
        Err(MemoryError::NotFound)
    ));
    let mut sending = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock_shared(72104643)")
        .execute(&mut *sending)
        .await?;
    assert!(matches!(
        database.forget_telegram_analysis_context(binding).await,
        Err(MemoryError::Busy)
    ));
    assert_eq!(
        legacy_state(&pool, before).await?,
        Value::Object(before.clone())
    );
    sending.rollback().await?;
    let epoch = database.forget_telegram_analysis_context(binding).await?;
    assert_ne!(epoch, original_epoch);

    let mut legacy = legacy_state(&pool, before).await?;
    assert_eq!(legacy["analysis:conversation"]["epoch"], epoch.to_string());
    legacy["analysis:conversation"]["epoch"] = json!("rotated");
    assert_eq!(legacy, fixture["after"]);
    let current: (Uuid, String, bool) = sqlx::query_as(
        "SELECT memory_epoch,state::text,share_owner_memory FROM conversations WHERE id=$1",
    )
    .bind(telegram)
    .fetch_one(&pool)
    .await?;
    assert_eq!(current, (epoch, "{}".into(), true));
    let unchanged: (Uuid, String) =
        sqlx::query_as("SELECT memory_epoch,state::text FROM conversations WHERE id=$1")
            .bind(other)
            .fetch_one(&pool)
            .await?;
    assert_eq!(unchanged.0, original_epoch);
    assert_eq!(
        serde_json::from_str::<Value>(&unchanged.1)?,
        json!({"analysis_turns":[independent]})
    );
    println!("Rotated both epochs, redacted four legacy answers and preserved unrelated work");
    Ok(())
}

async fn legacy_state(pool: &PgPool, keys: &Map<String, Value>) -> Result<Value, Box<dyn Error>> {
    let keys: Vec<&str> = keys.keys().map(String::as_str).collect();
    let text: String = sqlx::query_scalar(
        "SELECT jsonb_object_agg(key,value)::text FROM app_state WHERE key=ANY($1::text[])",
    )
    .bind(keys)
    .fetch_one(pool)
    .await?;
    Ok(serde_json::from_str(&text)?)
}
