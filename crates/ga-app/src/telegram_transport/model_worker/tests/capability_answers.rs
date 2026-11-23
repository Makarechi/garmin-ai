use super::*;
use ga_db::{
    diary::InterpretationContextQuery,
    share_policy::{DestinationKind, ShareDestination},
    web_chat::{
        WebChatInterpretationContext, WebChatModelPreparation, WebChatModelReply, WebChatSubmission,
    },
};

struct CapabilityModel {
    question: &'static str,
    language: &'static str,
    answer: Option<&'static str>,
    calls: AtomicUsize,
}

impl Sender for CapabilityModel {
    async fn send(
        &self,
        request: &PreparedRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        assert!(!remaining.is_zero());
        let PreparedRequest::Gemini(request) = request else {
            return Err(ModelTransportError::InvalidConfiguration);
        };
        assert_eq!(self.calls.fetch_add(1, Ordering::SeqCst), 0);
        let prompt: Value = serde_json::from_str(
            request.body()["input"]
                .as_str()
                .ok_or(ModelTransportError::InvalidConfiguration)?,
        )
        .map_err(|_| ModelTransportError::InvalidConfiguration)?;
        assert_eq!(prompt["text"], self.question);
        assert!(
            request.body()["system_instruction"]
                .as_str()
                .is_some_and(|instruction| instruction.contains("/history opens the diary"))
        );
        let result = json!({"intent":"question","confidence":0.97,
            "response_language":self.language,"capability_answer":self.answer});
        let response = json!({"status":"completed","steps":[{"type":"model_output",
            "content":[{"type":"text","text":result.to_string()}]}]});
        request
            .decode_response(response.to_string().as_bytes())
            .map_err(ModelTransportError::Response)
    }
}

#[tokio::test]
#[ignore = "uses authenticated Telegram ingress and a disposable TimescaleDB"]
async fn publishes_multilingual_capability_answers_with_one_model_call()
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
        (203, "¿Cuánto duró mi último entrenamiento?", "es", None),
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
        if let Some(answer) = answer {
            let worker = ModelDiaryWorker {
                database: &database,
                configured: &configured,
                gateway: &gateway,
                sender: &sender,
                timezone: "UTC",
                locale: "en",
            };
            worker.process(&lease, now).await;
            let (body, protected, completed): (String, bool, bool) = sqlx::query_as(
                "SELECT o.intent#>>'{blocks,0,text}',o.memory_fence IS NOT NULL,
                        i.status='processed' AND j.status='done'
                 FROM outbox_messages o JOIN inbound_messages i ON i.id=o.inbound_message_id
                 JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id)
                 WHERE i.normalized_text=$1",
            )
            .bind(question)
            .fetch_one(&pool)
            .await?;
            assert_eq!(body, answer);
            assert!(protected && completed);
        } else {
            let command = gateway
                .interpret_telegram_diary_with(
                    &lease,
                    "UTC",
                    now,
                    &mut AnalysisBudget::new(Instant::now()),
                    || configured.current(),
                    &sender,
                )
                .await?;
            assert!(matches!(
                publish_telegram_interpretation(
                    &database,
                    &lease,
                    &command,
                    DiaryReplyPreferences {
                        configured_timezone: "UTC",
                        owner_locale: "en",
                        started: now,
                        undo_anchor: None,
                    },
                )
                .await?,
                DiaryReplyOutcome::NeedsAnalysis
            ));
        }
        assert_eq!(sender.calls.load(Ordering::SeqCst), 1);
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
