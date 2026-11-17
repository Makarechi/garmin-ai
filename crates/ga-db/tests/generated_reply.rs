use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    conversation_memory::MemoryFence,
    outbox_enqueue::{QueueIntent, queue_generated_outbox_intent},
};
use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "compares generated reply fences on disposable pinned TimescaleDB"]
async fn queues_only_current_generation_and_pins_the_python_key() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected synthetic fixture")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let target: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(target, expected);

    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_GENERATED_REPLY")?)?)?;
    let now: StoredInstant = fixture["now"].as_str().ok_or("Missing time")?.parse()?;
    let conversation = uuid(&fixture["target"])?;
    let operation = uuid(&fixture["operation"])?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let cases = fixture["results"]
        .as_array()
        .ok_or("Missing generation cases")?;
    let first = cases.first().ok_or("Missing first generation")?;
    let initial = fence(&first["request_fence"])?;
    for (id, epoch, channel, instance) in
        std::iter::once((conversation, initial.target_epoch, "web", "local")).chain(
            initial
                .source_epochs
                .iter()
                .enumerate()
                .map(|(index, (id, epoch))| {
                    (
                        *id,
                        *epoch,
                        "telegram",
                        if index == 0 { "primary" } else { "fallback" },
                    )
                }),
        )
    {
        sqlx::query(
            "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
             VALUES($1,$2,$3,$4,$5,'{}',true)",
        ).bind(id).bind(owner).bind(channel).bind(instance).bind(epoch).execute(&pool).await?;
    }
    insert_inbound(
        &pool,
        uuid(&first["inbound"])?,
        owner,
        conversation,
        operation,
        1,
    )
    .await?;
    let edited = cases.last().ok_or("Missing edited case")?;
    for case in cases {
        let name = case["name"].as_str().ok_or("Missing case name")?;
        if name == "old_revision" {
            insert_inbound(
                &pool,
                uuid(&edited["inbound"])?,
                owner,
                conversation,
                operation,
                2,
            )
            .await?;
        }
        let mut input = case["intent"].clone();
        input["owner_id"] = json!(owner.to_string());
        let mut txn = pool.begin().await?;
        let receipt = queue_generated_outbox_intent(
            &mut txn,
            QueueIntent {
                intent: OutboundIntent::normalize(input)?,
                operation_id: operation,
                inbound_id: Some(uuid(&case["inbound"])?),
                dedup_key: None,
                requirements: &[],
                now,
            },
            &fence(&case["request_fence"])?,
        )
        .await?;
        txn.commit().await?;
        let stored = if let Some(receipt) = receipt {
            assert_eq!(receipt.created, name != "same_revision");
            let data: String = sqlx::query_scalar(
                "SELECT jsonb_build_object('id',id,'key',dedup_key,'fence',memory_fence)::text
                 FROM outbox_messages WHERE id=$1",
            )
            .bind(receipt.outbox_id)
            .fetch_one(&pool)
            .await?;
            serde_json::from_str(&data)?
        } else {
            Value::Null
        };
        assert_eq!(stored, case["row"], "{name}");
    }
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM outbox_messages")
        .fetch_one(&pool)
        .await?;
    assert_eq!(count, 2);
    let failing_id = Uuid::from_u128(2280);
    let failing_message = Uuid::from_u128(2281);
    let other_operation = Uuid::from_u128(2282);
    insert_inbound(
        &pool,
        failing_message,
        owner,
        conversation,
        other_operation,
        3,
    )
    .await?;
    sqlx::query(
        "ALTER TABLE outbox_messages ADD CONSTRAINT synthetic_fence_failure
           CHECK(memory_fence IS NULL OR id <> '00000000-0000-0000-0000-0000000008e8')",
    )
    .execute(&pool)
    .await?;
    let input = OutboundIntent::normalize(json!({
        "intent_id":failing_id.to_string(), "owner_id":owner.to_string(),
        "conversation_id":conversation.to_string(),
        "channel_instance":{"channel":"web","instance_id":"local"},
        "blocks":[{"text":"synthetic failing reply"}],
        "actions":[{"action_id":"fixture","label":"Confirm","operation_id":other_operation.to_string()}]
    }))?;
    let mut txn = pool.begin().await?;
    assert!(matches!(
        queue_generated_outbox_intent(
            &mut txn,
            QueueIntent {
                intent: input,
                operation_id: other_operation,
                inbound_id: Some(failing_message),
                dedup_key: None,
                requirements: &[],
                now,
            },
            &initial
        )
        .await,
        Err(ga_db::outbox_enqueue::QueueIntentError::Database)
    ));
    sqlx::query("INSERT INTO app_state(key,value) VALUES('synthetic-unrelated-change','{}')")
        .execute(&mut *txn)
        .await?;
    txn.commit().await?;
    let (outbox_count, token_count): (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM outbox_messages),
                (SELECT count(*) FROM app_state WHERE starts_with(key,'action-token:'))",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!((outbox_count, token_count), (2, 0));
    println!(
        "{} paired generation fences and queue keys matched",
        cases.len()
    );
    Ok(())
}

async fn insert_inbound(
    pool: &PgPool,
    id: Uuid,
    owner: Uuid,
    conversation: Uuid,
    operation: Uuid,
    revision: i32,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,external_event_id,
            sender_ref,received_at,kind,normalized_text,envelope,revision,status,operation_id)
         VALUES($1,$2,$3,'web','local',$4,'fixture','2026-11-17T12:00:00Z','text','synthetic question',
            '{}',$5,'processed',$6)",
    ).bind(id).bind(owner).bind(conversation).bind(format!("synthetic-{revision}")).bind(revision).bind(operation)
     .execute(pool).await?;
    Ok(())
}

fn uuid(value: &Value) -> Result<Uuid, Box<dyn Error>> {
    Ok(value.as_str().ok_or("Missing fixture UUID")?.parse()?)
}

fn fence(value: &Value) -> Result<MemoryFence, Box<dyn Error>> {
    let source_epochs = value["source_epochs"]
        .as_object()
        .ok_or("Missing source epochs")?
        .iter()
        .map(|(source, epoch)| Ok((source.parse()?, uuid(epoch)?)))
        .collect::<Result<_, Box<dyn Error>>>()?;
    Ok(MemoryFence {
        target_epoch: uuid(&value["target_epoch"])?,
        source_epochs,
    })
}
