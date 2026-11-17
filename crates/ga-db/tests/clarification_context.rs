use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{ClarificationClock, ClarificationError, ClarificationReply},
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Reference {
    seed: serde_json::Map<String, Value>,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    kind: String,
    destination: String,
    message_at: String,
    conversation_now: String,
    use_message_time: bool,
    text: Option<String>,
    value: Value,
}

#[tokio::test]
#[ignore = "compares legacy pending forms and transcript history in a disposable database"]
async fn keeps_recent_owner_clarification_in_its_channel() -> Result<(), Box<dyn Error>> {
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
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let reference: Reference =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_CLARIFICATION_CONTEXT")?)?)?;
    for (key, value) in reference.seed {
        sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
            .bind(key)
            .bind(value.to_string())
            .execute(&pool)
            .await?;
    }
    let mut bindings = std::collections::BTreeMap::new();
    for (channel, instance) in [
        ("telegram", "primary"),
        ("telegram", "secondary"),
        ("web", "primary"),
    ] {
        let conversation_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
             VALUES($1,$2,$3,$4,gen_random_uuid(),'{}',false)",
        ).bind(conversation_id).bind(owner).bind(channel).bind(instance).execute(&pool).await?;
        bindings.insert(
            format!("{channel}:{instance}"),
            ConversationBinding {
                owner_id: owner,
                conversation_id,
            },
        );
    }
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    for (index, case) in reference.cases.iter().enumerate() {
        let binding = bindings[&case.destination];
        let clock = ClarificationClock {
            message_at: case.message_at.parse()?,
            conversation_now: case.conversation_now.parse()?,
            use_message_time: case.use_message_time,
        };
        if case.kind == "write" {
            database
                .save_diary_clarification(
                    binding,
                    ClarificationReply {
                        clock,
                        text: case.text.as_deref().ok_or("Missing write text")?,
                        question: "synthetic question",
                        selection_prompt: None,
                    },
                )
                .await?;
            let key = if case.destination == "telegram:primary" {
                "conversation:pending".to_owned()
            } else {
                format!("conversation:pending:{}", case.destination)
            };
            let actual: String =
                sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                    .bind(key)
                    .fetch_one(&pool)
                    .await?;
            assert_eq!(
                serde_json::from_str::<Value>(&actual)?,
                case.value,
                "write {index}"
            );
        } else {
            let actual = database.pending_diary_clarification(binding, clock).await?;
            assert_eq!(actual.unwrap_or(Value::Null), case.value, "read {index}");
        }
    }
    let primary = bindings["telegram:primary"];
    let foreign = ConversationBinding {
        owner_id: Uuid::from_u128(1),
        ..primary
    };
    assert!(matches!(
        database.cancel_diary_clarification(foreign).await,
        Err(ClarificationError::MissingConversation)
    ));
    assert!(database.cancel_diary_clarification(primary).await?);
    assert!(!database.cancel_diary_clarification(primary).await?);
    let channels: Vec<String> = sqlx::query_scalar(
        "SELECT key FROM app_state WHERE starts_with(key,'conversation:pending') ORDER BY key",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        channels,
        [
            "conversation:pending:telegram:secondary",
            "conversation:pending:web:primary"
        ]
    );
    println!(
        "{} pending-form operations matched pinned Python with independent cancellation",
        reference.cases.len()
    );
    Ok(())
}
