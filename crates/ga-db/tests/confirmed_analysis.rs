use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    conversation_memory::{ConfirmedAnalysis, ConversationBinding, MemoryError, MemoryFence},
    delivery::{DeliveryReceipt, DeliveryState, DeliveryTarget, record_delivery_receipt},
};
use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

#[tokio::test]
#[ignore = "compares confirmed memory paths with the pinned Python service on disposable TimescaleDB"]
async fn remembers_only_exact_confirmed_and_current_answers() -> Result<(), Box<dyn Error>> {
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
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_CONFIRMED_ANALYSIS")?)?)?;
    let now: StoredInstant = string(&fixture["now"])?.parse()?;
    let binding = ConversationBinding {
        owner_id,
        conversation_id: identifier(&fixture["target"])?,
    };
    let baseline_fence = parse_fence(&fixture["fence"])?;
    let source = identifier(&fixture["source"])?;
    for (id, epoch, channel, instance, state) in [
        (
            binding.conversation_id,
            baseline_fence.target_epoch,
            "web",
            "local",
            json!({"pending":{"fixture":true}}),
        ),
        (
            source,
            baseline_fence.source_epochs[&source],
            "telegram",
            "primary",
            json!({}),
        ),
    ] {
        sqlx::query(
            "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
             VALUES($1,$2,$3,$4,$5,$6::jsonb,true)",
        ).bind(id).bind(owner_id).bind(channel).bind(instance).bind(epoch).bind(state.to_string())
         .execute(&pool).await?;
    }
    let messages = fixture["messages"]
        .as_array()
        .ok_or("Missing message fixtures")?;
    for (index, message) in messages.iter().enumerate() {
        seed_reply(&pool, binding, message, index).await?;
    }
    let events = fixture["events"].as_array().ok_or("Missing cases")?;
    for event in events {
        let index = event["index"].as_u64().ok_or("Missing message index")? as usize;
        let message = &messages[index];
        let outbox_id = identifier(&message["outbox_id"])?;
        let operation_id = identifier(&message["operation_id"])?;
        if !event["state"].is_null() {
            let state: DeliveryState = serde_json::from_value(event["state"].clone())?;
            let mut txn = pool.begin().await?;
            record_delivery_receipt(
                &mut txn,
                DeliveryTarget {
                    owner_id,
                    outbox_id,
                    lease_token: None,
                },
                DeliveryReceipt {
                    id: Uuid::new_v4(),
                    intent_id: outbox_id,
                    state,
                    observed_at: string(&event["observed_at"])?.parse()?,
                    provider_reference: None,
                    detail: None,
                },
            )
            .await?;
            txn.commit().await?;
        }
        let fence = parse_fence(&event["fence"])?;
        let result = database
            .remember_confirmed_analysis(ConfirmedAnalysis {
                binding,
                operation_id,
                outbox_id,
                fence: &fence,
                now,
                question: string(&message["question"])?,
                answer: string(&event["answer"])?,
            })
            .await;
        let result = match result {
            Ok(recorded) => json!(recorded),
            Err(MemoryError::Unconfirmed) => json!("Conflict"),
            Err(error) => return Err(error.into()),
        };
        assert_eq!(result, event["result"], "{}", event["name"]);
        let state: String = sqlx::query_scalar("SELECT state::text FROM conversations WHERE id=$1")
            .bind(binding.conversation_id)
            .fetch_one(&pool)
            .await?;
        assert_eq!(
            serde_json::from_str::<Value>(&state)?,
            event["stored"],
            "{}",
            event["name"]
        );
    }

    let last = messages
        .last()
        .ok_or("Expected at least one confirmed reply")?;
    let outbox_id = identifier(&last["outbox_id"])?;
    sqlx::query("DELETE FROM message_delivery_receipts WHERE outbox_message_id=$1")
        .bind(outbox_id)
        .execute(&pool)
        .await?;
    assert!(matches!(
        database
            .remember_confirmed_analysis(ConfirmedAnalysis {
                binding,
                operation_id: identifier(&last["operation_id"])?,
                outbox_id,
                fence: &baseline_fence,
                now,
                question: string(&last["question"])?,
                answer: string(&last["answer"])?,
            })
            .await,
        Err(MemoryError::Unconfirmed)
    ));
    println!(
        "{} paired confirmed-answer cases matched, including exact text, age and history fences",
        events.len()
    );
    Ok(())
}

async fn seed_reply(
    pool: &PgPool,
    binding: ConversationBinding,
    message: &Value,
    index: usize,
) -> Result<(), Box<dyn Error>> {
    let inbound = identifier(&message["inbound_id"])?;
    let operation = identifier(&message["operation_id"])?;
    let outbox = identifier(&message["outbox_id"])?;
    let at: StoredInstant = string(&message["received_at"])?.parse()?;
    let revisions = if message["edited"].as_bool() == Some(true) {
        2
    } else {
        1
    };
    for revision in 1..=revisions {
        let id = if revision == 1 {
            inbound
        } else {
            Uuid::from_u128(inbound.as_u128() + 2)
        };
        sqlx::query(
            "INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,external_event_id,
               sender_ref,received_at,kind,normalized_text,envelope,revision,status,operation_id)
             VALUES($1,$2,$3,'web','local',$4,'synthetic-sender',$5,'text',$6,'{}',$7,'processed',$8)",
        ).bind(id).bind(binding.owner_id).bind(binding.conversation_id).bind(index.to_string())
         .bind(OffsetDateTime::from(at)).bind(string(&message["question"])?).bind(revision)
         .bind(operation).execute(pool).await?;
    }
    let mut intent = message["intent"].clone();
    intent["owner_id"] = json!(binding.owner_id.to_string());
    let intent = OutboundIntent::normalize(intent)?;
    sqlx::query(
        "INSERT INTO outbox_messages(id,owner_id,conversation_id,inbound_message_id,operation_id,
             intent,memory_fence,dedup_key,state,attempts)
         VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,'queued',0)",
    )
    .bind(outbox)
    .bind(binding.owner_id)
    .bind(binding.conversation_id)
    .bind(inbound)
    .bind(operation)
    .bind(intent.document().to_string())
    .bind(message["fence"].to_string())
    .bind(format!("synthetic:{outbox}"))
    .execute(pool)
    .await?;
    Ok(())
}

fn string(value: &Value) -> Result<&str, Box<dyn Error>> {
    value
        .as_str()
        .ok_or_else(|| "Missing fixture string".into())
}

fn identifier(value: &Value) -> Result<Uuid, Box<dyn Error>> {
    Ok(string(value)?.parse()?)
}

fn parse_fence(value: &Value) -> Result<MemoryFence, Box<dyn Error>> {
    let source_epochs = value["source_epochs"]
        .as_object()
        .ok_or("Missing source epochs")?
        .iter()
        .map(|(source, epoch)| Ok((source.parse()?, identifier(epoch)?)))
        .collect::<Result<_, Box<dyn Error>>>()?;
    Ok(MemoryFence {
        target_epoch: identifier(&value["target_epoch"])?,
        source_epochs,
    })
}
