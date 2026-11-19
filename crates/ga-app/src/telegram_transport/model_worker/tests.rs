use std::{
    env,
    error::Error,
    str::FromStr,
    sync::atomic::{AtomicUsize, Ordering},
};

use ga_db::{
    delivery::{DeliveryReceipt, DeliveryState, DeliveryTarget, record_delivery_receipt},
    jobs::telegram_inbound::{LocalTelegramText, TelegramSafetyDecision},
    outbox_claims::{ReplyDestination, ReplyDispatchResult},
};
use ga_domain::model_response::StructuredModelOutput;
use ga_model::ModelTransportError;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

use super::*;
mod tracker;
mod voice;
use crate::{
    model_gateway::{PreparedRequest, RuntimeModelSettings},
    telegram_input::TelegramBinding,
};

const HEALTH_QUESTION: &str = "What is the synthetic Garmin sleep baseline?";
const FRESHNESS_QUESTION: &str = "How recent is the synthetic Garmin sleep summary?";
const SLEEP_QUESTION: &str = "When did the synthetic sleep interval start?";
const SLEEP_DAY: &str = "2024-02-18";
const SLEEP_START: &str = "2024-02-17T22:15:02.119000+00:00";
const SLEEP_END: &str = "2024-02-18T06:23:08+00:00";

struct SyntheticProvider {
    calls: AtomicUsize,
    created: String,
    from: String,
    to: String,
}

impl Sender for SyntheticProvider {
    async fn send(
        &self,
        input: &PreparedRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        assert!(!remaining.is_zero());
        let PreparedRequest::Gemini(request) = input else {
            return Err(ModelTransportError::InvalidConfiguration);
        };
        let prompt: Value = serde_json::from_str(
            request.body()["input"]
                .as_str()
                .ok_or(ModelTransportError::InvalidConfiguration)?,
        )
        .map_err(|_| ModelTransportError::InvalidConfiguration)?;
        let result = match self.calls.fetch_add(1, Ordering::SeqCst) {
            0 => {
                assert_eq!(prompt["text"], "Save a synthetic note now");
                json!({"intent":"log","confidence":0.99,"response_language":"en",
                    "events":[{"start":self.created,"timezone":"UTC","payload":{"type":"note","description":"Synthetic detail"}}]})
            }
            1 => {
                assert_eq!(prompt["text"], "What is in the synthetic diary?");
                json!({"intent":"question","confidence":1.0,"response_language":"en"})
            }
            call @ (2 | 5) => {
                let question = if call == 2 {
                    "What is in the synthetic diary?"
                } else {
                    "What about the previous entry?"
                };
                assert_eq!(prompt["question"], question);
                assert_eq!(prompt["tools"][0]["name"], "events");
                assert_eq!(prompt["evidence"], json!([]));
                if call == 5 {
                    assert!(
                        prompt["conversation"]
                            .to_string()
                            .contains("What is in the synthetic diary?")
                    );
                }
                json!({"calls":[{"name":"events","arguments_json":json!({"start":self.from,"end":self.to,"kind":"note"}).to_string()}]})
            }
            3 | 6 => {
                assert!(
                    prompt["evidence"][0]["result"]
                        .to_string()
                        .contains("Synthetic detail")
                );
                json!({"answer":"The diary contains the synthetic detail.","evidence_ids":[1],"response_language":"en"})
            }
            4 => {
                assert_eq!(prompt["text"], "What about the previous entry?");
                assert!(prompt["context"]["pending_clarification"].is_null());
                assert!(
                    !prompt
                        .to_string()
                        .contains("Synthetic local-only tracker schema")
                );
                assert_eq!(
                    prompt["context"]["recent_analysis_question"]["question"],
                    "What is in the synthetic diary?"
                );
                json!({"intent":"question","confidence":1.0,"response_language":"en"})
            }
            7 => {
                assert_eq!(prompt["text"], HEALTH_QUESTION);
                json!({"intent":"question","confidence":1.0,"response_language":"en"})
            }
            8 => {
                assert_eq!(prompt["question"], HEALTH_QUESTION);
                assert!(prompt["tools"].as_array().is_some_and(|tools| {
                    tools.iter().any(|tool| {
                        tool["name"] == "personal_baseline"
                            && tool["schema"]["required"] == json!(["metric", "start", "end"])
                    })
                }));
                json!({"calls":[{"name":"personal_baseline","arguments_json":json!({
                    "metric":"sleep_score","start":"2024-01-01","end":"2024-01-02"
                }).to_string()}]})
            }
            9 => {
                assert_eq!(prompt["evidence"][0]["result"]["mean"], 70.0);
                json!({"answer":"Here is the synthetic Garmin baseline.","evidence_ids":[1],
                       "numeric_claims":[{"evidence_id":1,"path":["mean"],"value":70}],
                       "response_language":"en"})
            }
            10 => {
                assert_eq!(prompt["text"], SLEEP_QUESTION);
                json!({"intent":"question","confidence":1.0,"response_language":"en"})
            }
            11 => {
                assert_eq!(prompt["question"], SLEEP_QUESTION);
                assert!(prompt["tools"].as_array().is_some_and(|tools| {
                    tools.iter().any(|tool| tool["name"] == "analysis_sleep")
                }));
                json!({"calls":[{"name":"analysis_sleep","arguments_json":json!({
                    "start":SLEEP_DAY,"end":SLEEP_DAY,"timing_only":true
                }).to_string()}]})
            }
            12 => {
                let evidence = &prompt["evidence"][0]["result"];
                assert_eq!(evidence["status"], "available");
                let start = &evidence["rows"][0]["main_interval"]["start"];
                assert_eq!(*start, SLEEP_START);
                json!({"evidence_ids":[1],"timestamp_claims":[{
                    "evidence_id":1,"path":["rows",0,"main_interval","start"],"value":start
                }],"response_language":"en"})
            }
            13 => {
                assert_eq!(prompt["text"], FRESHNESS_QUESTION);
                json!({"intent":"question","confidence":1.0,"response_language":"en"})
            }
            14 => {
                assert_eq!(prompt["question"], FRESHNESS_QUESTION);
                assert_eq!(prompt["quality_context"], json!({}));
                assert_eq!(prompt["planning_only"], true);
                json!({"calls":[{"name":"data_freshness","arguments_json":"{}"}]})
            }
            15 => {
                assert_eq!(
                    prompt["quality_context"]["sleep_score"]["semantics"],
                    "daily_summary"
                );
                assert_eq!(
                    prompt["evidence"][0]["result"]["channels"]["sleep_score"],
                    prompt["quality_context"]["sleep_score"]
                );
                assert!(prompt["evidence"][0]["result"].get("source_ref").is_none());
                json!({"answer":"The synthetic summary is saved.","evidence_ids":[1],
                       "include_current_state_freshness":true,"response_language":"en"})
            }
            _ => panic!("Unexpected provider retry"),
        };
        let response = json!({"status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":result.to_string()}]}]});
        request
            .decode_response(response.to_string().as_bytes())
            .map_err(ModelTransportError::Response)
    }
}

#[tokio::test]
#[ignore = "uses an isolated migrated TimescaleDB and a synthetic model"]
async fn runs_screened_telegram_logging_and_cited_analysis_through_the_worker()
-> Result<(), Box<dyn Error>> {
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
    sqlx::query("INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
                VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')").bind(owner_id).execute(&pool).await?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let started = StoredInstant::try_from(at)?;
    let before = StoredInstant::try_from(at - time::Duration::minutes(10))?.to_string();
    let after = StoredInstant::try_from(at + time::Duration::minutes(10))?.to_string();
    let created =
        StoredInstant::try_from(OffsetDateTime::from_unix_timestamp(at.unix_timestamp())?)?
            .to_string();
    let model=RuntimeModelSettings::resolve(|name| Ok(match name {
        "GA_LLM_ENABLED"=>Some("true".into()),
        "GA_LLM_CONSENT"=>Some(json!({"provider":"gemini","provider_instance_id":"model:gemini:primary",
            "model":"synthetic-model","categories":["health","diary"],"granted_at":before,"policy_revision":1}).to_string()),
        "GA_GEMINI_API_KEY"=>Some("synthetic-gemini".into()),
        "GA_GEMINI_MODEL"=>Some("synthetic-model".into()),
        _=>None,
    }), |_|Ok(None))?.ok_or("Missing fixture model")?;
    let configured = model.configured();
    let gateway = configured.gateway(database.clone(), false, started)?;
    let fake = SyntheticProvider {
        calls: AtomicUsize::new(0),
        created,
        from: before,
        to: after,
    };
    let worker = ModelDiaryWorker {
        database: &database,
        configured: &configured,
        gateway: &gateway,
        sender: &fake,
        timezone: "UTC",
        locale: "en",
    };
    let mut runtime = database.acquire_runtime_lease().await?;
    let binding = TelegramBinding {
        owner_id,
        instance_id: "primary",
        external_owner_id: 42,
    };
    let target = TelegramInboundTarget {
        owner_id,
        instance_id: "primary",
    };
    let process = async |runtime: &mut ga_db::runtime_lease::RuntimeLease, id: i64, text: &str| {
        let message_at = at.unix_timestamp() + i64::from(id == 3);
        binding
            .accept_polled_update(
                runtime,
                &json!({"update_id":id,"message":{
                    "message_id":id,"from":{"id":42},"chat":{"id":42,"type":"private"},
                    "text":text,"date":message_at,
                    "reply_to_message":(id==3).then(||json!({"message_id":810004}))
                }}),
                started,
                false,
            )
            .await?;
        assert!(
            database
                .claim_telegram_model_text(target, started, LEASE)
                .await?
                .is_none()
        );
        let screen = database
            .claim_telegram_local_screen(target, started, LEASE)
            .await?
            .ok_or("Expected unchecked report")?;
        database
            .apply_local_telegram_text(
                &screen,
                LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
                "en",
            )
            .await?;
        let lease = database
            .claim_telegram_model_text(target, started, LEASE)
            .await?
            .ok_or("Expected screened report")?;
        worker.process(&lease, started).await;
        Ok::<(), Box<dyn Error>>(())
    };
    for (id, text) in [
        (1, "Save a synthetic note now"),
        (2, "What is in the synthetic diary?"),
    ] {
        process(&mut runtime, id, text).await?;
    }
    assert_eq!(fake.calls.load(Ordering::SeqCst), 4);
    let stored: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events WHERE payload->>'description'='Synthetic detail'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(stored, 1);
    let replies:Vec<(String,bool)>=sqlx::query_as("SELECT intent#>>'{blocks,0,text}',
        coalesce(memory_fence ? 'telegram_diary_analysis',false) FROM outbox_messages ORDER BY created_at,id")
        .fetch_all(&pool).await?;
    assert_eq!(replies.len(), 2);
    assert!(replies[0].0.starts_with("Saved: note ("));
    assert!(!replies[0].1);
    assert_eq!(
        replies[1],
        (
            "The diary contains the synthetic detail.\n\nBased on saved diary data.".into(),
            true
        )
    );
    let (outbox_id, conversation_id): (Uuid, Uuid) = sqlx::query_as(
        "SELECT id,conversation_id FROM outbox_messages WHERE memory_fence ? 'telegram_diary_analysis'",
    ).fetch_one(&pool).await?;
    let conversation = ConversationBinding {
        owner_id,
        conversation_id,
    };
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let observed = StoredInstant::try_from(now)?;
    for state in [
        DeliveryState::ProviderAccepted,
        DeliveryState::Delivered,
        DeliveryState::Read,
    ] {
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
                observed_at: observed,
                provider_reference: Some("810004"),
                detail: None,
            },
        )
        .await?;
        txn.commit().await?;
        let memory = database.conversation_memory(conversation, observed).await?;
        assert_eq!(memory.turns.len(), usize::from(state.confirms_delivery()));
        if state.confirms_delivery() {
            assert_eq!(
                memory.turns[0]["question"],
                "What is in the synthetic diary?"
            );
            assert_eq!(memory.turns[0]["answer"], replies[1].0);
        }
    }
    sqlx::query("UPDATE conversations SET state=jsonb_set(state,'{analysis_turns}','[]'::jsonb) WHERE id=$1")
        .bind(conversation_id).execute(&pool).await?;
    let kind = env::var("GA_TEST_ANALYSIS_TRACKER_KIND").unwrap_or_else(|_| "selection".into());
    assert!(matches!(kind.as_str(), "selection" | "legacy" | "guided"));
    let mut pending = json!({"button":"tracker_select","question":"Synthetic local-only tracker schema",
        "options":[{"id":format!("create:{}",Uuid::nil()),"label":"Synthetic local-only tracker schema","definition_version_id":Uuid::nil().to_string()}],
        "created_at":started.to_string(),"prompt_order":[0,999999],"channel_instance_id":"telegram:primary"});
    if kind != "selection" {
        pending["button"] = json!("tracker_form");
        pending["definition_version_id"] = json!(Uuid::nil().to_string());
        if kind == "guided" {
            pending["chat_form"] =
                json!({"action_id":format!("create:{}",Uuid::nil()),"locale":"en"});
        }
    }
    sqlx::query("INSERT INTO app_state(key,value) VALUES('conversation:pending',$1::jsonb)")
        .bind(pending.to_string())
        .execute(&pool)
        .await?;
    process(&mut runtime, 3, "What about the previous entry?").await?;
    assert_eq!(fake.calls.load(Ordering::SeqCst), 7);
    let continued: (String, bool) = sqlx::query_as(
        "SELECT o.intent#>>'{blocks,0,text}',
        coalesce(o.memory_fence ? 'telegram_diary_analysis',false)
        FROM outbox_messages o JOIN inbound_messages i ON i.id=o.inbound_message_id
        WHERE i.normalized_text='What about the previous entry?'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(continued, (replies[1].0.clone(), true));
    let retained: String =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='conversation:pending'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(serde_json::from_str::<Value>(&retained)?, pending);
    if kind == "selection" {
        sqlx::query("UPDATE app_state SET value=value||'{\"prompt_order\":[0,0]}'::jsonb WHERE key='conversation:pending'").execute(&pool).await?;
        process(&mut runtime, 4, "1").await?;
        assert_eq!(fake.calls.load(Ordering::SeqCst), 7);
        let still_pending: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='conversation:pending')",
        )
        .fetch_one(&pool)
        .await?;
        assert!(!still_pending);
    }
    assert_eq!(
        database
            .conversation_memory(conversation, observed)
            .await?
            .turns
            .len(),
        1
    );
    database.forget_conversation_memory(conversation).await?;
    assert!(
        database
            .conversation_memory(conversation, observed)
            .await?
            .turns
            .is_empty()
    );
    println!(
        "Analysis replies bypassed the {kind} tracker prompt without exposing its schema in seven SDK calls"
    );
    sqlx::query(
        "INSERT INTO health_days(day,sleep_score,sources)
         VALUES ('2024-01-01',60,'{}'),('2024-01-02',80,'{}')",
    )
    .execute(&pool)
    .await?;
    database.scenario_packs(owner_id, true).await?;
    let allow_sleep =
        "UPDATE module_configs SET llm_enabled=$2 WHERE owner_id=$1 AND pack_key='sleep'";
    assert_eq!(
        sqlx::query(allow_sleep)
            .bind(owner_id)
            .bind(true)
            .execute(&pool)
            .await?
            .rows_affected(),
        1
    );
    process(&mut runtime, 5, HEALTH_QUESTION).await?;
    assert_eq!(fake.calls.load(Ordering::SeqCst), 10);
    let (reply_id, body, proof): (Uuid, String, String) = sqlx::query_as(
        "SELECT o.id,o.intent#>>'{blocks,0,text}',(o.memory_fence->'telegram_diary_analysis')::text
         FROM outbox_messages o JOIN inbound_messages i ON i.id=o.inbound_message_id
         WHERE i.normalized_text=$1",
    )
    .bind(HEALTH_QUESTION)
    .fetch_one(&pool)
    .await?;
    let proof: Value = serde_json::from_str(&proof)?;
    assert!(
        body.contains("/mean: 70") && body.contains("saved Garmin data"),
        "{body}"
    );
    assert_eq!(proof["reads"][0]["query"]["tool"], "personal_baseline");
    sqlx::query("UPDATE outbox_messages SET state='cancelled' WHERE id<>$1 AND state='queued'")
        .bind(reply_id)
        .execute(&pool)
        .await?;
    let destination = || ReplyDestination {
        owner_id,
        channel: "telegram",
        instance_id: "primary",
    };
    let claim = database
        .claim_outbox_reply(destination(), started, LEASE)
        .await?
        .ok_or("Expected claimed health reply")?;
    assert_eq!(claim.outbox_id, reply_id);
    sqlx::query(allow_sleep)
        .bind(owner_id)
        .bind(false)
        .execute(&pool)
        .await?;
    assert_eq!(
        database
            .dispatch_telegram_outbox_reply(destination(), 42, claim, &[], |_| async {
                panic!("Revoked health evidence escaped to Telegram")
            })
            .await?,
        ReplyDispatchResult::Cancelled
    );

    sqlx::query(allow_sleep)
        .bind(owner_id)
        .bind(true)
        .execute(&pool)
        .await?;
    seed_sleep_timing(&pool).await?;
    process(&mut runtime, 6, SLEEP_QUESTION).await?;
    assert_eq!(fake.calls.load(Ordering::SeqCst), 13);
    let (reply_id, body): (Uuid, String) = sqlx::query_as(
        "SELECT o.id,o.intent#>>'{blocks,0,text}' FROM outbox_messages o
         JOIN inbound_messages i ON i.id=o.inbound_message_id WHERE i.normalized_text=$1",
    )
    .bind(SLEEP_QUESTION)
    .fetch_one(&pool)
    .await?;
    assert!(
        body.contains("22:15") && body.contains("saved Garmin data"),
        "{body}"
    );
    let claim = database
        .claim_outbox_reply(destination(), started, LEASE)
        .await?
        .ok_or("Expected claimed sleep reply")?;
    assert_eq!(claim.outbox_id, reply_id);
    sqlx::query("UPDATE timeline_intervals SET confirmed=false WHERE id=$1")
        .bind(format!("sleep:{SLEEP_DAY}"))
        .execute(&pool)
        .await?;
    assert_eq!(
        database
            .dispatch_telegram_outbox_reply(destination(), 42, claim, &[], |_| async {
                panic!("Invalid sleep evidence escaped to Telegram")
            })
            .await?,
        ReplyDispatchResult::Cancelled
    );
    sqlx::query("INSERT INTO health_days(day,sleep_score,sources) VALUES($1,80,'{}')")
        .bind(at.date())
        .execute(&pool)
        .await?;
    sqlx::query(allow_sleep)
        .bind(owner_id)
        .bind(true)
        .execute(&pool)
        .await?;
    process(&mut runtime, 7, FRESHNESS_QUESTION).await?;
    assert_eq!(fake.calls.load(Ordering::SeqCst), 16);
    let (reply, body, proof): (Uuid, String, String) = sqlx::query_as(
        "SELECT o.id,o.intent#>>'{blocks,0,text}',(o.memory_fence->'telegram_diary_analysis')::text
         FROM outbox_messages o JOIN inbound_messages i ON i.id=o.inbound_message_id
         WHERE i.normalized_text=$1",
    )
    .bind(FRESHNESS_QUESTION)
    .fetch_one(&pool)
    .await?;
    assert!(
        body.contains("Data recency at the time of your question:"),
        "{body}"
    );
    let proof: Value = serde_json::from_str(&proof)?;
    assert_eq!(proof["reads"][0]["query"]["tool"], "data_freshness");
    assert!(proof["reads"][0]["checked_at"].is_string());
    let claim = database
        .claim_outbox_reply(destination(), started, LEASE)
        .await?
        .ok_or("Expected claimed freshness reply")?;
    assert_eq!(claim.outbox_id, reply);
    sqlx::query(allow_sleep)
        .bind(owner_id)
        .bind(false)
        .execute(&pool)
        .await?;
    assert_eq!(
        database
            .dispatch_telegram_outbox_reply(destination(), 42, claim, &[], |_| async {
                panic!("Revoked freshness evidence escaped to Telegram")
            })
            .await?,
        ReplyDispatchResult::Cancelled
    );
    Ok(())
}

async fn seed_sleep_timing(pool: &PgPool) -> Result<(), Box<dyn Error>> {
    let start = OffsetDateTime::from(SLEEP_START.parse::<StoredInstant>()?);
    let end = OffsetDateTime::from(SLEEP_END.parse::<StoredInstant>()?);
    let milliseconds = |at: OffsetDateTime| i64::try_from(at.unix_timestamp_nanos() / 1_000_000);
    let payload = json!({"dailySleepDTO":{"calendarDate":SLEEP_DAY,
        "sleepStartTimestampGMT":milliseconds(start)?, "sleepEndTimestampGMT":milliseconds(end)?}});
    let source = Uuid::from_u128(760);
    let hash = "a".repeat(64);
    let mut txn = pool.begin().await?;
    sqlx::query("INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,payload,fetched_at,parser_version,status)
        VALUES($1,'garmin_connect','sleep',$2,$3,'synthetic/sleep.json',$4::jsonb,$5,$6,'normalized')")
        .bind(source).bind(SLEEP_DAY).bind(&hash).bind(payload.to_string()).bind(end)
        .bind(ga_db::replay::GARMIN_PARSER_VERSION)
        .execute(&mut *txn).await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
        .bind(format!("ingest:garmin_connect:sleep:{SLEEP_DAY}"))
        .bind(
            json!({"source_ref":source.to_string(),"hash":hash,"status":"normalized"}).to_string(),
        )
        .execute(&mut *txn)
        .await?;
    sqlx::query("INSERT INTO timeline_intervals(id,start,\"end\",label,source,confidence,confirmed,evidence)
                 VALUES($1,$2,$3,'sleep','garmin_connect',1,true,$4::jsonb)")
        .bind(format!("sleep:{SLEEP_DAY}")).bind(start).bind(end)
        .bind(json!({"source_ref":source.to_string()}).to_string()).execute(&mut *txn).await?;
    txn.commit().await?;
    Ok(())
}
