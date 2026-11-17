use std::{collections::BTreeMap, env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    conversation_memory::{ConversationBinding, MemoryError},
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB and the pinned Python conversation fixtures"]
async fn matches_scoped_conversation_memory_and_forget() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected synthetic URL")?;
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
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid synthetic connection")?;
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_CONVERSATION_MEMORY")?)?)?;
    let now: StoredInstant = fixture["now"]
        .as_str()
        .ok_or("Missing fixture time")?
        .parse()?;
    for row in fixture["seed"].as_array().ok_or("Missing seed")? {
        sqlx::query(
            "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
             VALUES($1,$2,$3,$4,$5,$6::jsonb,$7)",
        )
        .bind(identifier(&row["id"])?).bind(owner_id)
        .bind(row["channel"].as_str()).bind(row["instance"].as_str())
        .bind(identifier(&row["epoch"])?).bind(row["state"].to_string())
        .bind(row["shared"].as_bool()).execute(&pool).await?;
    }
    let mut epochs = BTreeMap::new();
    let events = fixture["events"].as_array().ok_or("Missing events")?;
    for (index, event) in events.iter().enumerate() {
        let binding = ConversationBinding {
            owner_id,
            conversation_id: identifier(&event["id"])?,
        };
        let operation = event["operation"].as_str().ok_or("Missing operation")?;
        let result = match operation {
            "snapshot" => {
                let snapshot = database.conversation_memory(binding, now).await?;
                let sources: BTreeMap<_, _> = snapshot
                    .fence
                    .source_epochs
                    .iter()
                    .map(|(source, epoch)| (source.to_string(), epoch.to_string()))
                    .collect();
                json!({"turns":snapshot.turns, "target_epoch":snapshot.fence.target_epoch.to_string(), "source_epochs":sources})
            }
            "sharing" | "forget" => {
                let epoch = if operation == "forget" {
                    database.forget_conversation_memory(binding).await?
                } else {
                    let selected = event["data"]
                        .as_bool()
                        .ok_or("Missing sharing preference")?;
                    database
                        .set_conversation_memory_sharing(binding, selected)
                        .await?
                }
                .to_string();
                let expected = event["result"].as_str().ok_or("Missing expected epoch")?;
                if let Some(previous) = epochs.insert(expected, epoch.clone()) {
                    assert_eq!(previous, epoch, "unchanged choice must keep the epoch");
                }
                json!(epoch)
            }
            "append" => {
                let turn = rewrite_epochs(&event["data"], &epochs)?;
                sqlx::query(
                    "UPDATE conversations SET state=jsonb_set(state,'{analysis_turns}',
                        coalesce(state->'analysis_turns','[]'::jsonb) || $2::jsonb)
                     WHERE id=$1",
                )
                .bind(binding.conversation_id)
                .bind(json!([turn]).to_string())
                .execute(&pool)
                .await?;
                Value::Null
            }
            _ => return Err("Unknown fixture operation".into()),
        };
        assert_eq!(
            result,
            rewrite_epochs(&event["result"], &epochs)?,
            "{index} {operation}"
        );
        let rows: String = sqlx::query_scalar(
            "SELECT jsonb_agg(jsonb_build_object('id',id,'epoch',memory_epoch,
                'state',state,'shared',share_owner_memory) ORDER BY id)::text FROM conversations",
        )
        .fetch_one(&pool)
        .await?;
        assert_eq!(
            serde_json::from_str::<Value>(&rows)?,
            rewrite_epochs(&event["rows"], &epochs)?,
            "{index} {operation} stored rows"
        );
    }

    let conversation_id = identifier(&fixture["seed"][0]["id"])?;
    let foreign = ConversationBinding {
        owner_id: Uuid::from_u128(1),
        conversation_id,
    };
    assert!(matches!(
        database.conversation_memory(foreign, now).await,
        Err(MemoryError::NotFound)
    ));
    assert!(matches!(
        database.forget_conversation_memory(foreign).await,
        Err(MemoryError::NotFound)
    ));
    assert!(matches!(
        database
            .set_conversation_memory_sharing(foreign, false)
            .await,
        Err(MemoryError::NotFound)
    ));
    let mut writer = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *writer)
        .await?;
    let wanted = ConversationBinding {
        owner_id,
        conversation_id,
    };
    assert!(matches!(
        database.conversation_memory(wanted, now).await,
        Err(MemoryError::Busy)
    ));
    writer.rollback().await?;

    println!(
        "{} paired conversation snapshots and retained state transitions matched",
        events.len()
    );
    Ok(())
}

fn identifier(value: &Value) -> Result<Uuid, Box<dyn Error>> {
    Ok(value.as_str().ok_or("Missing UUID")?.parse()?)
}

fn rewrite_epochs(
    value: &Value,
    epochs: &BTreeMap<&str, String>,
) -> Result<Value, serde_json::Error> {
    let mut text = value.to_string();
    for (reference, observed) in epochs {
        text = text.replace(reference, observed);
    }
    serde_json::from_str(&text)
}
