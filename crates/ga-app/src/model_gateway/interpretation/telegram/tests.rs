use std::{env, error::Error, time::Instant};

use serde_json::json;
use time::OffsetDateTime;

use super::{super::EXTRACT, *};

#[test]
fn extracts_unique_explicit_records_with_exact_hex_boundaries() -> Result<(), Box<dyn Error>> {
    let first = Uuid::from_u128(42);
    let second = Uuid::from_u128(43);
    let ignored = Uuid::from_u128(44);
    let text = format!("test({first}), {first} {second:X} 1{ignored} {ignored}a");
    assert_eq!(mentions(&text), [first, second]);
    let many = (0..24)
        .map(|number| Uuid::from_u128(number).to_string())
        .collect::<Vec<_>>()
        .join(" ");
    assert_eq!(mentions(&many).len(), 21);
    Ok(())
}

#[test]
fn narrows_to_complete_current_selection() -> Result<(), Box<dyn Error>> {
    let first = Uuid::from_u128(42).to_string();
    let other = Uuid::from_u128(43).to_string();
    let now: StoredInstant = "2026-02-01T00:00:00+00:00".parse()?;
    let entries = [
        json!({"id": other, "revision": 1}),
        json!({"id": first, "revision": 2}),
    ];
    let mut pending = json!({
        "action": "close", "explicit_selector": true, "event_ids": [first],
        "selection_revision": 2, "selection_expires_at": "2026-02-01T00:10:00+00:00",
    });
    assert_eq!(
        pending_selection(&entries, &pending, now),
        PendingSelection::Selected(vec![entries[1].clone()]),
    );
    pending["selection_revision"] = json!(1);
    assert!(matches!(
        pending_selection(&entries, &pending, now),
        PendingSelection::Expired,
    ));
    pending["explicit_selector"] = json!(false);
    pending["targets_complete"] = json!(false);
    assert_eq!(
        pending_selection(&entries, &pending, now),
        PendingSelection::Any
    );
    Ok(())
}

#[tokio::test]
#[ignore = "exercises native authenticated Telegram interpretation against disposable TimescaleDB"]
async fn uses_current_native_message_context_and_queues_its_clarification()
-> Result<(), Box<dyn Error>> {
    use std::{
        str::FromStr,
        sync::atomic::{AtomicUsize, Ordering},
        time::Duration,
    };

    use ga_db::{Database, jobs::telegram_inbound::TelegramInboundTarget};
    use ga_domain::model_response::StructuredModelOutput;
    use ga_model::ModelTransportError;
    use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

    use crate::model_gateway::{ConfiguredModel, PreparedRequest, RuntimeModelSettings};
    use crate::telegram_input::TelegramBinding;

    struct SyntheticModel {
        calls: AtomicUsize,
    }

    impl Sender for SyntheticModel {
        async fn send(
            &self,
            request: &PreparedRequest,
            remaining: Duration,
        ) -> Result<StructuredModelOutput, ModelTransportError> {
            assert!(!remaining.is_zero());
            self.calls.fetch_add(1, Ordering::SeqCst);
            let PreparedRequest::Gemini(request) = request else {
                return Err(ModelTransportError::InvalidConfiguration);
            };
            assert_eq!(request.body()["system_instruction"], EXTRACT.trim());
            let prompt: Value = request.body()["input"]
                .as_str()
                .and_then(|input| serde_json::from_str(input).ok())
                .ok_or(ModelTransportError::InvalidConfiguration)?;
            assert_eq!(prompt["text"], "Synthetic reply");
            assert_eq!(prompt["source"], "telegram_text");
            assert_eq!(prompt["timezone"], "Europe/Bratislava");
            assert_eq!(
                prompt["context"]["pending_clarification"]["text"],
                "Synthetic original report"
            );
            assert_eq!(prompt["context"]["recent_events"], json!([]));
            let response = serde_json::to_vec(&json!({
                "status":"completed",
                "steps":[{
                    "type":"model_output",
                    "content":[{
                        "type":"text",
                        "text":"{\"intent\":\"clarify\",\"confidence\":0.91,\"clarification\":\"What time did it start?\"}"
                    }]
                }],
            })).map_err(|_| ModelTransportError::InvalidConfiguration)?;
            request
                .decode_response(&response)
                .map_err(ModelTransportError::Response)
        }
    }

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
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner_id).execute(&pool).await?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(at)?;
    let earlier = StoredInstant::try_from(at - time::Duration::seconds(60))?.to_string();
    sqlx::query("INSERT INTO app_state(key,value) VALUES('conversation:pending',$1::jsonb)")
        .bind(
            json!({
                "text":"Synthetic original report","question":"Please clarify",
                "created_at":earlier,"channel_instance_id":"telegram:primary"
            })
            .to_string(),
        )
        .execute(&pool)
        .await?;
    let binding = TelegramBinding {
        owner_id,
        instance_id: "primary",
        external_owner_id: 42,
    };
    let mut runtime = database.acquire_runtime_lease().await?;
    let ingress = binding
        .accept_polled_update(
            &mut runtime,
            &json!({
                "update_id":301,"message":{
                    "message_id":761,"from":{"id":42},"chat":{"id":42,"type":"private"},
                    "text":"Synthetic reply","date":at.unix_timestamp()
                }
            }),
            now,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing authenticated inbox message")?;
    let lease = database
        .claim_telegram_inbound(
            TelegramInboundTarget {
                owner_id,
                instance_id: "primary",
            },
            now,
            60,
        )
        .await?
        .ok_or("Missing authenticated lease")?;
    let model = RuntimeModelSettings::resolve(
        |name| {
            Ok(match name {
                "GA_LLM_ENABLED" => Some("true".into()),
                "GA_LLM_CONSENT" => Some(
                    json!({
                        "provider":"gemini","provider_instance_id":"model:gemini:primary",
                        "model":"synthetic-model","categories":["health","diary"],
                        "granted_at":earlier,"policy_revision":1
                    })
                    .to_string(),
                ),
                "GA_GEMINI_API_KEY" => Some("synthetic-gemini".into()),
                "GA_GEMINI_MODEL" => Some("synthetic-model".into()),
                _ => None,
            })
        },
        |_| Ok(None),
    )?
    .ok_or("Missing synthetic model")?;
    let configuration: ConfiguredModel<'_> = model.configured();
    let gateway = configuration.gateway(database.clone(), false, now)?;
    let model = SyntheticModel {
        calls: AtomicUsize::new(0),
    };
    let input = || TelegramText::from_lease(&lease, "Europe/Bratislava", now);
    let mut budget = AnalysisBudget::new(Instant::now());
    let request = gateway
        .interpret_telegram_with(
            &lease,
            input()?,
            &mut budget,
            &|| configuration.current(),
            &model,
        )
        .await?;
    assert_eq!(request.command.intent(), AgentIntent::Clarify);
    let question = request
        .command
        .clarification()
        .ok_or("Missing model clarification")?;
    let queued = database
        .apply_telegram_text_clarification(&lease, now, question)
        .await?;
    let actual: (String,String) = sqlx::query_as(
        "SELECT inbox.status, reply.intent#>>'{blocks,0,text}'
           FROM inbound_messages inbox JOIN outbox_messages reply ON reply.inbound_message_id=inbox.id
          WHERE inbox.id=$1 AND reply.id=$2",
    ).bind(ingress.message_id).bind(queued.outbox_id).fetch_one(&pool).await?;
    assert_eq!(
        actual,
        ("processed".into(), "What time did it start?".into())
    );
    assert!(
        gateway
            .interpret_telegram_with(
                &lease,
                input()?,
                &mut budget,
                &|| configuration.current(),
                &model,
            )
            .await
            .is_err()
    );
    assert_eq!(model.calls.load(Ordering::SeqCst), 1);
    Ok(())
}
