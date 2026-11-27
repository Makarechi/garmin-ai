use super::*;
use ga_db::{
    diary::InterpretationContextQuery,
    outbox_claims::{ReplyObservation, ReplySendOutcome},
    share_policy::{DestinationKind, ShareDestination},
    web_chat::{
        WebChatInterpretationContext, WebChatModelPreparation, WebChatModelReply, WebChatSubmission,
    },
};

const SLEEP_REVIEW_QUESTION: &str = "Расскажи подробно, как я спал вчера и сегодня";
const SLEEP_INTERPRETATION: &str =
    "Сегодня сон был заметно дольше. Стоит понаблюдать за временем отхода ко сну.";

struct CapabilityModel {
    question: &'static str,
    language: &'static str,
    answer: Option<&'static str>,
    calls: AtomicUsize,
}

impl Sender for CapabilityModel {
    #[expect(
        clippy::unused_async_trait_impl,
        reason = "Keep the async trait method deferred until the returned future is polled"
    )]
    async fn send(
        &self,
        request: &PreparedRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        assert!(!remaining.is_zero());
        let PreparedRequest::Gemini(request) = request else {
            return Err(ModelTransportError::InvalidConfiguration);
        };
        let call = self.calls.fetch_add(1, Ordering::SeqCst);
        let prompt: Value = serde_json::from_str(
            request.body()["input"]
                .as_str()
                .ok_or(ModelTransportError::InvalidConfiguration)?,
        )
        .map_err(|_| ModelTransportError::InvalidConfiguration)?;
        let result = if self.question == SLEEP_REVIEW_QUESTION && call == 0 {
            assert_eq!(prompt["question"], self.question);
            assert_eq!(prompt["tools"], json!([]));
            assert_eq!(prompt["planning_only"], false);
            assert_eq!(prompt["answer_only"], true);
            assert_eq!(prompt["conversation"], json!({}));
            assert_eq!(prompt["evidence"].as_array().map(Vec::len), Some(1));
            assert_eq!(prompt["evidence"][0]["tool"], "analysis_sleep_review");
            let review = &prompt["evidence"][0]["result"];
            assert_eq!(review["rows"].as_array().map(Vec::len), Some(2));
            assert_eq!(
                review["rows"][0]["values"]["duration_minutes"].as_f64(),
                Some(400.0)
            );
            assert_eq!(
                review["rows"][1]["values"]["duration_minutes"].as_f64(),
                Some(520.0)
            );
            assert!(review["rows"][1]["main_interval"]["start"].is_string());
            assert_eq!(
                review["rows"][1]["change_from_previous_requested_night"]["duration_minutes"],
                120.0
            );
            assert_eq!(
                review["baseline"]["metrics"]["duration_minutes"]["nights"],
                27
            );
            assert_eq!(
                review["baseline"]["metrics"]["duration_minutes"]["median"],
                420.0
            );
            assert!(
                review["rows"][1]["deviations"]
                    .as_array()
                    .is_some_and(|values| values
                        .iter()
                        .any(|value| value["metric"] == "duration_minutes"
                            && value["direction"] == "above"))
            );
            json!({"answer":SLEEP_INTERPRETATION,"evidence_ids":[1],"response_language":"ru"})
        } else if self.answer.is_none() && call == 1 {
            assert_eq!(prompt["question"], self.question);
            return Err(ModelTransportError::Unavailable {
                reason: ModelUnavailability::Upstream,
                retry_after: Duration::from_secs(60),
                detail: ModelFailureDetail::HttpStatus(reqwest::StatusCode::SERVICE_UNAVAILABLE),
            });
        } else {
            assert_eq!(call, 0);
            assert_eq!(prompt["text"], self.question);
            assert!(
                request.body()["system_instruction"]
                    .as_str()
                    .is_some_and(|instruction| instruction.contains("/history opens the diary"))
            );
            json!({"intent":"question","confidence":0.97,
                "response_language":self.language,"capability_answer":self.answer})
        };
        let response = json!({"status":"completed","steps":[{"type":"model_output",
            "content":[{"type":"text","text":result.to_string()}]}]});
        request
            .decode_response(response.to_string().as_bytes())
            .map_err(ModelTransportError::Response)
    }
}

#[tokio::test]
#[ignore = "uses authenticated Telegram ingress and a disposable TimescaleDB"]
async fn publishes_multilingual_capability_answers_and_provider_errors()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, name);
    let owner_id: Uuid = sqlx::query_scalar(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         SELECT gen_random_uuid(),id,'telegram','primary','42','fixture' FROM people WHERE singleton
         RETURNING owner_id",
    ).fetch_one(&pool).await?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(at)?;
    let granted = StoredInstant::try_from(at - time::Duration::minutes(1))?;
    let model = RuntimeModelSettings::resolve(|name| Ok(match name {
        "GA_LLM_ENABLED" => Some("true".into()),
        "GA_LLM_CONSENT" => Some(json!({"provider":"gemini","provider_instance_id":"model:gemini:primary",
            "model":"synthetic-model","categories":["health","diary"],"granted_at":granted.to_string(),"policy_revision":1}).to_string()),
        "GA_GEMINI_API_KEY" => Some("synthetic-gemini".into()),
        "GA_GEMINI_MODEL" => Some("synthetic-model".into()),
        _ => None,
    }), |_| Ok(None))?.ok_or("Missing fixture model")?;
    let configured = model.configured();
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    database.scenario_packs(owner_id, true).await?;
    sqlx::query(
        "UPDATE module_configs SET llm_enabled=true WHERE owner_id=$1 AND pack_key='sleep'",
    )
    .bind(owner_id)
    .execute(&pool)
    .await?;
    seed_sleep_review(&pool, at.date()).await?;
    assert!(
        !database
            .replay_status(ga_db::replay::GARMIN_PARSER_VERSION)
            .await?
            .ready
    );
    let gateway = configured.gateway(database.clone(), false, now)?;
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
    for (id, question, language, answer) in [
        (
            201,
            "¿En qué me puedes ayudar con mis entrenos?",
            "es",
            Some("Puedo ayudarte a revisar las actividades sincronizadas con Garmin."),
        ),
        (
            202,
            "Ako mi pomôžeš viesť denník?",
            "sk",
            Some("Môžem zaznamenávať a spätne prehliadať záznamy v denníku."),
        ),
        (203, SLEEP_REVIEW_QUESTION, "ru", None),
        (204, "Сравни тестовый сон за прошлую неделю", "ru", None),
    ] {
        binding
            .accept_polled_update(
                &mut runtime,
                &json!({"update_id":id,"message":{
                    "message_id":id,"from":{"id":42},"chat":{"id":42,"type":"private"},
                    "text":question,"date":at.unix_timestamp()
                }}),
                now,
                false,
            )
            .await?;
        let screen = database
            .claim_telegram_local_screen(target, now, LEASE)
            .await?
            .ok_or("Expected unchecked question")?;
        database
            .apply_local_telegram_text(
                &screen,
                LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
                "en",
            )
            .await?;
        let lease = database
            .claim_telegram_model_text(target, now, LEASE)
            .await?
            .ok_or("Expected screened question")?;
        let sender = CapabilityModel {
            question,
            language,
            answer,
            calls: AtomicUsize::new(0),
        };
        let worker = ModelDiaryWorker {
            database: &database,
            configured: &configured,
            gateway: &gateway,
            sender: &sender,
            timezone: "UTC",
            locale: "en",
        };
        worker.process(&lease, now).await;
        let (reply, body, protected, completed): (Uuid, String, bool, bool) = sqlx::query_as(
            "SELECT o.id,o.intent#>>'{blocks,0,text}',o.memory_fence IS NOT NULL,
                    i.status='processed' AND j.status='done'
             FROM outbox_messages o JOIN inbound_messages i ON i.id=o.inbound_message_id
             JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id)
             WHERE i.normalized_text=$1",
        )
        .bind(question)
        .fetch_one(&pool)
        .await?;
        if question == SLEEP_REVIEW_QUESTION {
            assert!(body.contains("Сон 6 ч 40 мин") && body.contains("Сон 8 ч 40 мин"));
            assert!(body.contains("REM") && body.contains(SLEEP_INTERPRETATION));
            assert!(body.contains("Больше сна, чем в предыдущую ночь — 2 ч 0 мин"));
            assert!(body.contains("Медианная длительность сна — 7 ч 0 мин"));
        } else {
            let expected =
                answer.unwrap_or("Сервис модели временно недоступен (Gemini, HTTP 503).");
            assert_eq!(body, expected);
        }
        assert!(completed);
        assert_eq!(
            protected,
            answer.is_some() || question == SLEEP_REVIEW_QUESTION
        );
        assert_eq!(
            sender.calls.load(Ordering::SeqCst),
            if answer.is_some() || question == SLEEP_REVIEW_QUESTION {
                1
            } else {
                2
            }
        );
        let destination = || ReplyDestination {
            owner_id,
            channel: "telegram",
            instance_id: "primary",
        };
        let claim = database
            .claim_outbox_reply(destination(), now, LEASE)
            .await?
            .ok_or("Expected a queued reply")?;
        assert_eq!(claim.outbox_id, reply);
        assert!(matches!(
            database
                .dispatch_telegram_outbox_reply(
                    destination(),
                    42,
                    claim,
                    &[],
                    |intent| async move {
                        assert_eq!(intent["blocks"][0]["text"], body);
                        Some(ReplySendOutcome::Observation(ReplyObservation {
                            state: DeliveryState::ProviderAccepted,
                            observed_at: now,
                            provider_reference: Some((810_000 + id).to_string()),
                            detail: None,
                        }))
                    }
                )
                .await?,
            ReplyDispatchResult::Recorded {
                state: DeliveryState::ProviderAccepted,
                ..
            }
        ));
    }
    let writes: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
        .fetch_one(&pool)
        .await?;
    assert_eq!(writes, 0);

    let submission = WebChatSubmission {
        client_message_id: Uuid::new_v4(),
        text: "このアシスタントで何ができますか？",
        configured_timezone: "UTC",
        configured_locale: "en",
        urgent_messages: None,
    };
    let WebChatModelPreparation::Ready(session) =
        database.prepare_web_chat_model(&submission).await?
    else {
        return Err("Expected a new web chat message".into());
    };
    let model = ShareDestination::new(DestinationKind::Model, gateway.binding().instance_id)?;
    let mentioned_events = [];
    let digest = database
        .interpretation_context(InterpretationContextQuery {
            binding: session.binding(),
            model,
            now: session.at,
            clarification_clock: session.clarification_clock(),
            mentioned_events: &mentioned_events,
        })
        .await?
        .digest()?;
    let answer = "Garminのデータと日記を確認できます。";
    let mut snapshot = database
        .finish_web_chat_model(
            *session,
            "UTC",
            WebChatModelReply::CapabilityAnswer {
                answer,
                expected: WebChatInterpretationContext {
                    model,
                    digest: &digest,
                    mentioned_events: &mentioned_events,
                },
            },
            |_| panic!("Capability answers cannot make diary changes"),
        )
        .await?;
    let mut body = Vec::new();
    while let Some(chunk) = snapshot.next_json().await? {
        body.extend(chunk);
    }
    assert!(String::from_utf8(body)?.contains(answer));
    Ok(())
}

async fn seed_sleep_review(pool: &PgPool, today: time::Date) -> Result<(), sqlx::Error> {
    sqlx::query(
        "WITH nights AS MATERIALIZED (
            SELECT gen_random_uuid() AS id, $1::date-offset_days AS day,
                   CASE offset_days WHEN 0 THEN 520 WHEN 1 THEN 400 WHEN 30 THEN 999 ELSE 420 END AS minutes,
                   CASE offset_days WHEN 20 THEN $2-1 ELSE $2 END AS parser_version,
                   (($1::date-offset_days)::timestamp - interval '2 hours') AT TIME ZONE 'UTC' AS bedtime,
                   (($1::date-offset_days)::timestamp + interval '7 hours') AT TIME ZONE 'UTC' AS wake_time
            FROM generate_series(-1,30) AS offsets(offset_days)
        ), payloads AS (
            INSERT INTO source_payloads (id,source,endpoint,source_key,payload_hash,archive_key,payload,fetched_at,parser_version,status)
            SELECT id,'garmin_connect','sleep',day::text,repeat('b',64),'synthetic/sleep.json',
                   jsonb_build_object('dailySleepDTO',jsonb_build_object(
                       'calendarDate',day::text,'sleepStartTimestampGMT',(extract(epoch from bedtime)*1000)::bigint,
                       'sleepEndTimestampGMT',(extract(epoch from wake_time)*1000)::bigint)),
                   wake_time,parser_version,'normalized'
            FROM nights
        ), watermarks AS (
            INSERT INTO app_state(key,value)
            SELECT concat('ingest:garmin_connect:sleep:',day),
                   jsonb_build_object('source_ref',id::text,'hash',repeat('b',64),'status','normalized') FROM nights
        ), intervals AS (
            INSERT INTO timeline_intervals (id,start,\"end\",label,source,confidence,confirmed,evidence)
            SELECT concat('sleep:',day),bedtime,wake_time,'sleep','garmin_connect',1,true,
                   jsonb_build_object('source_ref',id::text) FROM nights
        )
        INSERT INTO health_days (day,sleep_seconds,sleep_score,deep_seconds,rem_seconds,light_seconds,awake_seconds,sources)
        SELECT day,minutes*60,81,3600,5400,(minutes-150)*60,1800,jsonb_build_object(
            'field:sleep_seconds',id::text,'field:sleep_score',id::text,'field:deep_seconds',id::text,
            'field:rem_seconds',id::text,'field:light_seconds',id::text,'field:awake_seconds',id::text)
        FROM nights",
    )
    .bind(today)
    .bind(ga_db::replay::GARMIN_PARSER_VERSION)
    .execute(pool)
    .await?;
    Ok(())
}
