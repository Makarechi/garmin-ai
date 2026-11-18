use std::{env, error::Error, fs, str::FromStr};

use ga_app::{telegram_input::TelegramBinding, telegram_reply::prepare_reply};
use ga_db::{
    Database,
    jobs::telegram_inbound::TelegramInboundTarget,
    outbox_enqueue::{QueueIntent, queue_outbox_intent},
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::{access::Scope, channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

#[tokio::test]
#[ignore = "compares pinned Python Telegram history on disposable TimescaleDB"]
async fn pages_history_with_signed_current_actions() -> Result<(), Box<dyn Error>> {
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
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    let binding = TelegramBinding {
        owner_id: owner,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let conversation = ga_domain::channel::telegram_conversation_id(owner, "primary", 42);
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(at)?;
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_HISTORY")?)?)?;
    assert_eq!(cases.len(), 3);
    let mut update = json!({
        "update_id":401,"message":{
            "message_id":61, "from":{"id":42}, "chat":{"id":42,"type":"private"},
            "text":"/history", "date":at.unix_timestamp()
        }
    });
    let mut shared_reply = None;
    for (index, case) in cases.iter().enumerate() {
        let receipt = binding
            .accept_polled_update(&mut runtime, &update, now, true)
            .await?
            .inbound
            .ok_or("Missing authenticated history update")?;
        let lease = database
            .claim_telegram_inbound(target, now, 60)
            .await?
            .ok_or("Missing history lease")?;
        assert_eq!(lease.message_id(), receipt.message_id);
        let queued = database.apply_telegram_history_page(&lease).await?;
        let intent: String =
            sqlx::query_scalar("SELECT intent::text FROM outbox_messages WHERE id=$1")
                .bind(queued.outbox_id)
                .fetch_one(&pool)
                .await?;
        let intent = OutboundIntent::normalize(serde_json::from_str(&intent)?)?;
        let reply = prepare_reply(binding, &intent, now)?;
        assert_eq!(json!(reply.parts.join("\n")), case["text"]);
        assert_eq!(
            json!(
                reply
                    .buttons
                    .iter()
                    .map(|button| &button.label)
                    .collect::<Vec<_>>()
            ),
            case["buttons"]
        );
        assert!(
            reply
                .buttons
                .iter()
                .all(|button| button.callback_data.len() <= 64)
        );
        assert!(database.apply_telegram_history_page(&lease).await.is_err());
        if index == 0 {
            let (definition, version): (Uuid, Uuid) = sqlx::query_as(
                "SELECT d.id,v.id FROM event_definitions d
                  JOIN event_definition_versions v ON v.definition_id=d.id
                 WHERE d.key='user.shared_history'",
            )
            .fetch_one(&pool)
            .await?;
            let references = intent.document()["evidence_refs"]
                .as_array()
                .ok_or("Missing tracker consent evidence")?;
            assert!(references.contains(&json!(format!("definition:{definition}"))));
            assert!(references.contains(&json!(format!("definition-version:{version}"))));
            shared_reply = Some((queued.outbox_id, definition));
            let next = reply.buttons.last().ok_or("Missing next button")?;
            assert_eq!(next.label, "Далее");
            update = callback(402, &next.callback_data);
        } else if index == 1 {
            let operation = Uuid::new_v4();
            let intent = OutboundIntent::normalize(json!({
                "owner_id":owner.to_string(), "conversation_id":conversation.to_string(),
                "channel_instance":{"channel":"telegram","instance_id":"primary"},
                "actions":[{"label":"Open history","action_id":"diary.history.open",
                            "operation_id":operation.to_string()}],
            }))?;
            let mut txn = pool.begin().await?;
            let queued = queue_outbox_intent(
                &mut txn,
                QueueIntent {
                    intent,
                    operation_id: operation,
                    inbound_id: None,
                    dedup_key: None,
                    requirements: &[],
                    now,
                },
            )
            .await?;
            let button: String = sqlx::query_scalar(
                "SELECT intent#>>'{actions,0,token}' FROM outbox_messages WHERE id=$1",
            )
            .bind(queued.outbox_id)
            .fetch_one(&mut *txn)
            .await?;
            txn.commit().await?;
            update = callback(403, &button);
        }
    }
    let (outbox, definition) = shared_reply.ok_or("Expected a history reply with a tracker")?;
    assert!(
        database
            .revoke_tracker_share(
                [Scope::Admin].into_iter().collect(),
                definition,
                ShareDestination::new(DestinationKind::Channel, "telegram:primary")?,
            )
            .await?
    );
    let state: String = sqlx::query_scalar("SELECT state FROM outbox_messages WHERE id=$1")
        .bind(outbox)
        .fetch_one(&pool)
        .await?;
    assert_eq!(state, "cancelled");
    Ok(())
}

fn callback(update_id: i64, token: &str) -> Value {
    json!({
        "update_id":update_id,
        "callback_query":{
            "id":format!("synthetic-history-{update_id}"), "from":{"id":42}, "data":token,
            "message":{"message_id":update_id,"chat":{"id":42,"type":"private"}}
        }
    })
}
