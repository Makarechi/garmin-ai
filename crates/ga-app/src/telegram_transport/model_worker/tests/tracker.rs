use std::fs;

use super::*;

mod atomic;
mod recovery;
#[path = "../../../../tests/support/tracker_snapshot.rs"]
mod tracker_snapshot;

struct LegacyTrackerProvider {
    calls: AtomicUsize,
    safety_outcome: AtomicUsize,
}

impl Sender for LegacyTrackerProvider {
    #[expect(
        clippy::unused_async_trait_impl,
        reason = "Keep the async trait method deferred until the returned future is polled"
    )]
    async fn send(
        &self,
        input: &PreparedRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        assert!(!remaining.is_zero());
        self.calls.fetch_add(1, Ordering::SeqCst);
        let PreparedRequest::Gemini(request) = input else {
            return Err(ModelTransportError::InvalidConfiguration);
        };
        let output = if request.body()["system_instruction"]
            == include_str!("../../../../../../src/garmin_ai/prompts/form_safety.txt").trim()
        {
            match self.safety_outcome.load(Ordering::SeqCst) {
                0 => json!({"urgent":false,"response_language":"en"}),
                1 => json!({"urgent":true,"response_language":"en"}),
                _ => return Err(ModelTransportError::RequestRejected),
            }
        } else {
            let prompt: Value = serde_json::from_str(
                request.body()["input"]
                    .as_str()
                    .ok_or(ModelTransportError::InvalidConfiguration)?,
            )
            .map_err(|_| ModelTransportError::InvalidConfiguration)?;
            let text = prompt["text"]
                .as_str()
                .ok_or(ModelTransportError::InvalidConfiguration)?;
            if text.contains("provider-unavailable") {
                return Err(ModelTransportError::RequestRejected);
            }
            assert_eq!(
                prompt["candidate_trackers"]
                    .as_array()
                    .ok_or(ModelTransportError::InvalidConfiguration)?
                    .len(),
                1
            );
            let candidate = &prompt["candidate_trackers"][0];
            assert!(candidate.get("events").is_none());
            assert!(candidate.get("original_text").is_none());
            let field = &candidate["fields"][0];
            let quote = text
                .split_whitespace()
                .last()
                .ok_or(ModelTransportError::InvalidConfiguration)?
                .trim_matches('"');
            let value = match field["schema"]["type"].as_str() {
                Some("integer") => json!(
                    quote
                        .parse::<i64>()
                        .map_err(|_| ModelTransportError::InvalidConfiguration)?
                ),
                Some("number") => json!(
                    quote
                        .parse::<f64>()
                        .map_err(|_| ModelTransportError::InvalidConfiguration)?
                ),
                Some("boolean") => json!(true),
                _ => json!(quote),
            };
            let begin = text
                .rfind(quote)
                .ok_or(ModelTransportError::InvalidConfiguration)?;
            let start = text[..begin].chars().count();
            let mut output = json!({
                "schema_version":"tracker.nl.v1", "intent":"create_entry",
                "definition_version_id":candidate["definition_version_id"], "event_id":null,
                "start":"2026-11-18T03:00:00+00:00", "end":null,
                "start_evidence":{"start":0,"end":text.chars().count(),"quote":text},
                "end_evidence":null,
                "fields":[{"field_id":field["field_id"],"value":value,
                    "evidence":{"start":start,"end":start+quote.chars().count(),"quote":quote},
                    "unit":null,"unit_evidence":null}],
                "tracker_draft":null,"clarification":null,"confidence":0.99
            });
            if text.contains("low-confidence") {
                output["confidence"] = json!(0.5);
            }
            if text.contains("clarify-response") {
                output["intent"] = json!("clarify");
                output["clarification"] = json!("Synthetic clarification");
            }
            if text.contains("none-response") {
                output["intent"] = json!("none");
            }
            if text.contains("invalid-quote") {
                output["fields"][0]["evidence"]["quote"] = json!("different synthetic quote");
            }
            output
        };
        let response = json!({"status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":output.to_string()}]}]});
        request
            .decode_response(response.to_string().as_bytes())
            .map_err(ModelTransportError::Response)
    }
}

#[tokio::test]
#[ignore = "compares selected legacy tracker messages with frozen Python on an exact disposable database"]
async fn legacy_tracker_model_messages_match_the_frozen_conversation() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let options =
        PgConnectOptions::from_str(&url.replace("postgresql+psycopg://", "postgresql://"))?
            .disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test") && name.len() <= 63)
        .ok_or("Expected disposable database")?
        .to_owned();
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect_with(options)
        .await?;
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT current_database()")
            .fetch_one(&pool)
            .await?,
        expected
    );
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let locale = env::var("GA_TEST_TRACKER_CHAT_LOCALE").unwrap_or_else(|_| "en".into());
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let started = StoredInstant::try_from(at)?;
    let settings = RuntimeModelSettings::resolve(
        |name| {
            Ok(match name {
                "GA_LLM_ENABLED" => Some("true".into()),
                "GA_GEMINI_API_KEY" => Some("synthetic-legacy-tracker".into()),
                "GA_GEMINI_MODEL" => Some("synthetic-legacy-tracker-model".into()),
                "GA_LLM_CONSENT" => Some(
                    json!({"provider":"gemini","provider_instance_id":"model:gemini:primary",
            "model":"synthetic-legacy-tracker-model","categories":["health","diary"],
            "granted_at":"2026-11-18T00:00:00+00:00","policy_revision":1})
                    .to_string(),
                ),
                _ => None,
            })
        },
        |_| Ok(None),
    )?
    .ok_or("Expected model settings")?;
    let configured = settings.configured();
    let gateway = configured.gateway(database.clone(), false, started)?;
    let provider = LegacyTrackerProvider {
        calls: AtomicUsize::new(0),
        safety_outcome: AtomicUsize::new(0),
    };
    let worker = ModelDiaryWorker {
        database: &database,
        configured: &configured,
        gateway: &gateway,
        sender: &provider,
        timezone: "UTC",
        locale: &locale,
    };
    let mut runtime = database.acquire_runtime_lease().await?;
    let binding = TelegramBinding {
        owner_id: owner,
        instance_id: "primary",
        external_owner_id: 42,
    };
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let cases: Vec<Value> = serde_json::from_str(&fs::read_to_string(env::var(
        "GA_TEST_TRACKER_CHAT_ENTRIES",
    )?)?)?;
    for (index, case) in cases.iter().enumerate() {
        let name = case["legacy_tracker"]
            .as_str()
            .ok_or("Expected legacy tracker")?;
        let version: Uuid = sqlx::query_scalar("SELECT v.id FROM event_definition_versions v
            JOIN event_definitions d ON d.id=v.definition_id AND d.current_version=v.version WHERE d.key=$1")
            .bind(format!("user.synthetic_chat_{name}")).fetch_one(&pool).await?;
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&pool)
            .await?;
        let now = StoredInstant::try_from(now)?;
        if case["grant"].is_array() {
            let definition: Uuid = sqlx::query_scalar(
                "SELECT definition_id FROM event_definition_versions WHERE id=$1",
            )
            .bind(version)
            .fetch_one(&pool)
            .await?;
            database
                .grant_tracker_share(
                    [ga_domain::access::Scope::ManageIntegrations]
                        .into_iter()
                        .collect(),
                    ga_db::share_policy::TrackerShareGrant {
                        definition_id: definition,
                        destination: ShareDestination::new(
                            DestinationKind::Channel,
                            "telegram:primary",
                        )?,
                        categories: &[ga_db::share_policy::ShareCategory::Schema],
                        granted_at: now,
                    },
                )
                .await?;
        }
        let pending = json!({"definition_version_id":version.to_string(),"button":"tracker_form","action":"log",
            "event_ids":[],"question":"Synthetic legacy tracker prompt","channel_instance_id":"telegram:primary","created_at":now.to_string()});
        sqlx::query(
            "INSERT INTO app_state(key,value) VALUES('conversation:pending',$1::jsonb)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        )
        .bind(pending.to_string())
        .execute(&pool)
        .await?;
        let id = 71001 + i64::try_from(index)?;
        let wire = json!({"update_id":id,"message":{"message_id":id,"from":{"id":42},"chat":{"id":42,"type":"private"},
            "date":case["source_message_date"],"text":case["message"]["text"]}});
        let receipt = binding
            .accept_polled_update(&mut runtime, &wire, now, true)
            .await?
            .inbound
            .ok_or("Expected new receipt")?;
        assert!(receipt.created);
        let screen = database
            .claim_telegram_local_screen(target, now, 150)
            .await?
            .ok_or("Expected local screen")?;
        database
            .apply_local_telegram_text(
                &screen,
                LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
                &locale,
            )
            .await?;
        let mut lease = database
            .claim_telegram_model_text(target, now, 150)
            .await?
            .ok_or("Expected model lease")?;
        if case["recover"] == true {
            sqlx::query("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1 AND lease_token=$2")
                .bind(lease.job_identity().0).bind(lease.job_identity().1).execute(&pool).await?;
            let before = provider.calls.load(Ordering::SeqCst);
            worker.process(&lease, now).await;
            assert_eq!(provider.calls.load(Ordering::SeqCst), before);
            let recovered_at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&pool)
                .await?;
            lease = database
                .claim_telegram_model_text(target, StoredInstant::try_from(recovered_at)?, 150)
                .await?
                .ok_or("Expected recovered model lease")?;
        }
        worker.process(&lease, now).await;
        let reply: String = sqlx::query_scalar(
            "SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE inbound_message_id=$1",
        )
        .bind(receipt.message_id)
        .fetch_one(&pool)
        .await?;
        assert_eq!(json!(reply), case["reply"], "Reply at step {index}");
        assert_eq!(
            tracker_snapshot::with_nl_state(&pool, &cases).await?,
            case["snapshot"],
            "State at step {index}"
        );
        let completed: (String, String) = sqlx::query_as(
            "SELECT i.status,j.status FROM inbound_messages i
            JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id) WHERE i.id=$1",
        )
        .bind(receipt.message_id)
        .fetch_one(&pool)
        .await?;
        assert_eq!(completed, ("processed".into(), "done".into()));
        let calls = provider.calls.load(Ordering::SeqCst);
        worker.process(&lease, now).await;
        assert_eq!(provider.calls.load(Ordering::SeqCst), calls);
        assert_eq!(
            tracker_snapshot::with_nl_state(&pool, &cases).await?,
            case["snapshot"]
        );
        assert!(
            !binding
                .accept_polled_update(&mut runtime, &wire, now, true)
                .await?
                .inbound
                .ok_or("Expected duplicate receipt")?
                .created
        );
    }
    let shareable = cases
        .iter()
        .filter(|case| case["legacy_tracker"] != "sensitive")
        .count();
    assert_eq!(provider.calls.load(Ordering::SeqCst), shareable * 2);
    atomic::assert_atomic_commit(&database, &pool, &mut runtime, &locale).await?;
    println!(
        "Matched {} legacy tracker model messages and {} SDK calls, including expired-lease recovery and duplicate delivery",
        cases.len(),
        provider.calls.load(Ordering::SeqCst)
    );
    Box::pin(recovery::assert_safety_recovery(
        &worker,
        &pool,
        &mut runtime,
    ))
    .await?;
    if let Ok(status) = fs::read_to_string("/proc/self/status") {
        for line in status
            .lines()
            .filter(|line| line.starts_with("VmHWM:") || line.starts_with("VmRSS:"))
        {
            println!("{line}");
        }
    }
    for name in ["memory.peak", "memory.swap.peak", "memory.events"] {
        if let Ok(value) = fs::read_to_string(format!("/sys/fs/cgroup/{name}")) {
            println!("{name}\n{value}");
        }
    }
    pool.close().await;
    Ok(())
}
