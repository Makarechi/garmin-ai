use ga_db::runtime_lease::RuntimeLease;
use ga_domain::channel::InboundKind;

use super::*;

const SPEECH: &str = "Save a synthetic voice note now";
const CAPTION: &str = "Synthetic caption for the voice note";
const SAFETY_MARKER: &str = "synthetic safety marker";

struct VoiceProvider<'a> {
    transcriptions: AtomicUsize,
    interpretations: AtomicUsize,
    safety_screens: AtomicUsize,
    fragment_delay: Duration,
    at: String,
    speech: &'a str,
}

impl Sender for VoiceProvider<'_> {
    async fn send(
        &self,
        input: &PreparedRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        assert!(!remaining.is_zero());
        let PreparedRequest::Gemini(request) = input else {
            return Err(ModelTransportError::InvalidConfiguration);
        };
        let body = request.body();
        assert_eq!(body["store"], false);
        let result = if body["input"].is_array() {
            self.transcriptions.fetch_add(1, Ordering::SeqCst);
            assert_eq!(body["input"][0]["content"][0]["type"], "audio");
            assert_eq!(body["input"][0]["content"][0]["data"], "T2dnUw==");
            assert_eq!(body["input"][0]["content"][0]["mime_type"], "audio/ogg");
            json!({"text":self.speech})
        } else {
            let input = body["input"]
                .as_str()
                .ok_or(ModelTransportError::InvalidConfiguration)?;
            if self.speech.len() > 16_000 {
                self.safety_screens.fetch_add(1, Ordering::SeqCst);
                assert!(input.chars().count() <= 12_256);
                if !self.fragment_delay.is_zero() {
                    tokio::time::sleep(self.fragment_delay).await;
                }
                json!({"urgent":input.contains(SAFETY_MARKER),"response_language":"en"})
            } else {
                self.interpretations.fetch_add(1, Ordering::SeqCst);
                let prompt: Value = serde_json::from_str(input)
                    .map_err(|_| ModelTransportError::InvalidConfiguration)?;
                assert_eq!(prompt["text"], format!("{SPEECH}\n{CAPTION}"));
                json!({"intent":"log","confidence":1.0,"response_language":"en",
                "events":[{"start":self.at,"timezone":"UTC","payload":{"type":"note","description":"Synthetic voice detail"}}]})
            }
        };
        request
            .decode_response(
                json!({"status":"completed","steps":[{"type":"model_output","content":[
                {"type":"text","text":result.to_string()}]}]})
                .to_string()
                .as_bytes(),
            )
            .map_err(ModelTransportError::Response)
    }
}

async fn clock(pool: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(pool)
        .await?;
    Ok(StoredInstant::try_from(at)?)
}

async fn set_audio_consent(pool: &PgPool, granted: bool) -> Result<(), sqlx::Error> {
    let mut model_categories = vec!["diary", "health"];
    if granted {
        model_categories.push("audio");
    }
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('preferences:onboarding',$1::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    .bind(
        json!({"channel":{"channel":"telegram","instance_id":"primary"},"model_categories":model_categories})
            .to_string(),
    )
    .execute(pool)
    .await?;
    Ok(())
}

fn update(id: i64, at: StoredInstant, seconds: u64, caption: &str) -> Value {
    json!({"update_id":id,"message":{"message_id":id,"date":OffsetDateTime::from(at).unix_timestamp(),
        "from":{"id":42},"chat":{"id":42,"type":"private"},"caption":caption,
        "voice":{"file_id":"synthetic_voice","file_unique_id":"synthetic_voice_unique",
            "duration":seconds,"file_size":4,"mime_type":"audio/ogg"}}})
}

async fn accept(
    binding: TelegramBinding<'_>,
    runtime: &mut RuntimeLease,
    wire: &Value,
    at: StoredInstant,
) -> Result<Uuid, Box<dyn Error>> {
    Ok(binding
        .accept_polled_update(runtime, wire, at, true)
        .await?
        .inbound
        .ok_or("Expected accepted fixture")?
        .message_id)
}

#[tokio::test]
#[ignore = "uses a migrated disposable database, a local Telegram simulation and a synthetic model"]
async fn runs_voice_ingress_transcription_and_cached_diary_processing() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let origin = env::var("GA_TEST_TELEGRAM_VOICE_ORIGIN")?;
    assert!(origin.starts_with("http://127.0.0.1:"));
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected test database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT current_database()")
            .fetch_one(&pool)
            .await?,
        name
    );
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let at = clock(&pool).await?;
    let before = StoredInstant::try_from(OffsetDateTime::from(at) - time::Duration::minutes(10))?;
    let created = StoredInstant::try_from(OffsetDateTime::from_unix_timestamp(
        OffsetDateTime::from(at).unix_timestamp(),
    )?)?;
    let model = RuntimeModelSettings::resolve(
        |name| {
            Ok(match name {
                "GA_LLM_ENABLED" => Some("true".into()),
                "GA_LLM_CONSENT" => Some(
                    json!({"provider":"gemini","provider_instance_id":"model:gemini:primary",
            "model":"synthetic-model","categories":["health","diary","audio"],
            "granted_at":before.to_string(),"policy_revision":1})
                    .to_string(),
                ),
                "GA_GEMINI_API_KEY" => Some("synthetic-gemini".into()),
                "GA_GEMINI_MODEL" => Some("synthetic-model".into()),
                _ => None,
            })
        },
        |_| Ok(None),
    )?
    .ok_or("Missing fixture model")?;
    let configured = model.configured();
    let gateway = configured.gateway(database.clone(), false, at)?;
    let sender = VoiceProvider {
        transcriptions: AtomicUsize::new(0),
        interpretations: AtomicUsize::new(0),
        safety_screens: AtomicUsize::new(0),
        fragment_delay: Duration::ZERO,
        at: created.to_string(),
        speech: SPEECH,
    };
    let worker = ModelDiaryWorker {
        database: &database,
        configured: &configured,
        gateway: &gateway,
        sender: &sender,
        timezone: "UTC",
        locale: "en",
    };
    let transport = TelegramTransport::connect("100:synthetic-voice", &origin, false)?;
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

    let raw = update(78001, at, 601, "");
    let oversized = accept(binding, &mut runtime, &raw, at).await?;
    let lease = database
        .claim_telegram_voice(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected oversized voice lease")?;
    worker.process_voice(&transport, &lease, at).await;
    let result: (String, String) = sqlx::query_as(
        "SELECT i.status,o.intent#>>'{blocks,0,text}'
        FROM inbound_messages i JOIN outbox_messages o ON i.id=o.inbound_message_id WHERE i.id=$1",
    )
    .bind(oversized)
    .fetch_one(&pool)
    .await?;
    assert_eq!(result.0, "invalid");
    assert!(result.1.contains("20 MB"));
    assert_eq!(sender.transcriptions.load(Ordering::SeqCst), 0);

    let wire = update(78002, at, 600, CAPTION);
    let message_id = accept(binding, &mut runtime, &wire, at).await?;
    assert!(
        database
            .claim_telegram_voice(target, clock(&pool).await?, LEASE)
            .await?
            .is_none()
    );
    let screen = database
        .claim_telegram_local_screen(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected caption screen")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await?;
    let local = database
        .claim_telegram_tracker_form(target, clock(&pool).await?, LEASE, true)
        .await?
        .ok_or("Expected local caption classification")?;
    database
        .apply_telegram_tracker_form(&local, "UTC", "en", true)
        .await?;
    let voice = database
        .claim_telegram_voice(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected eligible voice lease")?;
    worker
        .process_voice(&transport, &voice, clock(&pool).await?)
        .await;
    assert_eq!(sender.transcriptions.load(Ordering::SeqCst), 1);
    assert_eq!(sender.interpretations.load(Ordering::SeqCst), 0);
    let persisted: (String, String, String) = sqlx::query_as(
        "SELECT envelope->>'text', normalized_text, status
        FROM inbound_messages WHERE id=$1",
    )
    .bind(message_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        persisted,
        (
            CAPTION.into(),
            format!("{SPEECH}\n{CAPTION}"),
            "pending".into()
        )
    );
    assert_eq!(accept(binding, &mut runtime, &wire, at).await?, message_id);
    assert!(
        database
            .claim_telegram_voice(target, clock(&pool).await?, LEASE)
            .await?
            .is_none()
    );

    let abandoned = database
        .claim_telegram_local_screen(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected transcript screen")?;
    sqlx::query("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1")
        .bind(abandoned.job_identity().0)
        .execute(&pool)
        .await?;
    let recovered = database
        .claim_telegram_local_screen(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected recovered transcript screen")?;
    assert_eq!(recovered.processing_envelope()?.kind(), InboundKind::Voice);
    assert_eq!(
        recovered.processing_envelope()?.text(),
        Some(format!("{SPEECH}\n{CAPTION}").as_str())
    );
    database
        .apply_local_telegram_text(
            &recovered,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await?;
    let ready = database
        .claim_telegram_model_text(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected transcript interpretation")?;
    worker.process(&ready, clock(&pool).await?).await;
    assert_eq!(sender.transcriptions.load(Ordering::SeqCst), 1);
    assert_eq!(sender.interpretations.load(Ordering::SeqCst), 1);
    let events: Vec<(String, String)> = sqlx::query_as(
        "SELECT source,original_text FROM events
        WHERE kind='note' AND payload->>'description'='Synthetic voice detail'",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        events,
        vec![("telegram_voice".into(), format!("{SPEECH}\n{CAPTION}"))]
    );
    let reply_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM outbox_messages WHERE inbound_message_id=$1")
            .bind(message_id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(reply_count, 1);

    let revoked_wire = update(78003, at, 10, "");
    let revoked_id = accept(binding, &mut runtime, &revoked_wire, at).await?;
    let voice = database
        .claim_telegram_voice(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected voice before audio withdrawal")?;
    worker
        .process_voice(&transport, &voice, clock(&pool).await?)
        .await;
    assert_eq!(sender.transcriptions.load(Ordering::SeqCst), 2);
    set_audio_consent(&pool, false).await?;
    let screened = database
        .claim_telegram_local_screen(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected saved transcript before local screening")?;
    let result = database
        .apply_local_telegram_text(
            &screened,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await;
    assert!(
        matches!(result, Err(TelegramDiaryCommitError::VoiceConsentChanged)),
        "Expected the audio policy to reject saved text: {result:?}"
    );
    assert!(super::super::super::local_text::complete_or_retry(&database, &screened, result).await);
    let blocked: (String, Option<String>, String) = sqlx::query_as(
        "SELECT i.status, o.intent#>>'{blocks,0,text}', j.status
         FROM inbound_messages i JOIN outbox_messages o ON i.id=o.inbound_message_id
         JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id)
         WHERE i.id=$1",
    )
    .bind(revoked_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(blocked.0, "processed");
    assert_eq!(blocked.2, "done");
    let notice = blocked.1.ok_or("Expected voice withdrawal notice")?;
    assert!(!notice.contains(SPEECH));
    assert_eq!(sender.interpretations.load(Ordering::SeqCst), 1);
    set_audio_consent(&pool, true).await?;

    let speech = format!(
        "{}{} {}",
        "x".repeat(12_000),
        "y".repeat(12_600),
        SAFETY_MARKER
    );
    let long_sender = VoiceProvider {
        transcriptions: AtomicUsize::new(0),
        interpretations: AtomicUsize::new(0),
        safety_screens: AtomicUsize::new(0),
        fragment_delay: Duration::ZERO,
        at: created.to_string(),
        speech: &speech,
    };
    let long_worker = ModelDiaryWorker {
        sender: &long_sender,
        ..worker
    };
    let long_wire = update(78004, at, 10, "");
    let long_id = accept(binding, &mut runtime, &long_wire, at).await?;
    let voice = database
        .claim_telegram_voice(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected long voice lease")?;
    long_worker
        .process_voice(&transport, &voice, clock(&pool).await?)
        .await;
    let screen = database
        .claim_telegram_local_screen(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected long transcript local screen")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await?;
    let ready = database
        .claim_telegram_model_text(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected long transcript model screen")?;
    long_worker.process(&ready, clock(&pool).await?).await;
    assert_eq!(long_sender.transcriptions.load(Ordering::SeqCst), 1);
    assert_eq!(long_sender.safety_screens.load(Ordering::SeqCst), 3);
    assert_eq!(long_sender.interpretations.load(Ordering::SeqCst), 0);
    let notice: String = sqlx::query_scalar(
        "SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE inbound_message_id=$1",
    )
    .bind(long_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(notice, urgent_notice("en"));

    let race_update = update(78005, at, 10, "");
    let race_id = accept(binding, &mut runtime, &race_update, at).await?;
    let voice = database
        .claim_telegram_voice(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected voice before download race")?;
    let (listener, race_transport) = super::super::super::tests::bind().await?;
    let server = async {
        use super::super::super::tests::{request, respond};

        let (mut metadata, _) = listener.accept().await?;
        assert_eq!(request(&mut metadata).await?["endpoint"], "getFile");
        respond(
            &mut metadata,
            "200 OK",
            &[],
            r#"{"ok":true,"result":{"file_path":"voice/synthetic.oga","file_size":4}}"#,
        )
        .await?;
        let (mut audio, _) = listener.accept().await?;
        assert_eq!(request(&mut audio).await?["method"], "GET");
        set_audio_consent(&pool, false).await?;
        respond(&mut audio, "200 OK", &[], "OggS").await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let started = clock(&pool).await?;
    let (server, ()) = tokio::time::timeout(Duration::from_secs(8), async {
        tokio::join!(
            server,
            worker.process_voice(&race_transport, &voice, started)
        )
    })
    .await?;
    server?;
    let blocked: (String, i32) =
        sqlx::query_as("SELECT status,length(normalized_text) FROM inbound_messages WHERE id=$1")
            .bind(race_id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(blocked, ("invalid".into(), 0));
    assert_eq!(sender.transcriptions.load(Ordering::SeqCst), 2);
    set_audio_consent(&pool, true).await?;

    recorded_commands(&worker, &transport, binding, &mut runtime, &pool, at).await?;

    screen_with_short_voice_lease(&worker, &transport, binding, &mut runtime, &pool, at).await?;

    let limited_wire = update(78011, at, 10, "");
    let limited_id = accept(binding, &mut runtime, &limited_wire, at).await?;
    let transport = TelegramTransport::connect("100:synthetic-rate-limit", &origin, false)?;
    let limited = database
        .claim_telegram_voice(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected retryable voice")?;
    worker
        .process_voice(&transport, &limited, clock(&pool).await?)
        .await;
    let result:(String,String,bool)=sqlx::query_as("SELECT i.status,j.status,j.run_at>clock_timestamp()+interval '10 seconds'
        FROM inbound_messages i JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id) WHERE i.id=$1")
        .bind(limited_id).fetch_one(&pool).await?;
    assert_eq!(result, ("pending".into(), "pending".into(), true));
    assert_eq!(sender.transcriptions.load(Ordering::SeqCst), 2);
    println!(
        "Voice ingress, 601-second rejection, 429 delay, transcript recovery, revoked consent, bounded safety screening and one attributed diary write passed"
    );
    Ok(())
}

async fn recorded_commands(
    worker: &ModelDiaryWorker<'_, '_, VoiceProvider<'_>>,
    transport: &TelegramTransport,
    binding: TelegramBinding<'_>,
    runtime: &mut RuntimeLease,
    pool: &PgPool,
    at: StoredInstant,
) -> Result<(), Box<dyn Error>> {
    use ga_db::jobs::telegram_inbound::TelegramControlError;

    let database = worker.database;
    let target = TelegramInboundTarget {
        owner_id: binding.owner_id,
        instance_id: binding.instance_id,
    };
    for (id, command) in [
        (78006, "/pause"),
        (78007, "/resume"),
        (78008, "/history"),
        (78009, "/synthetic-unknown"),
    ] {
        let sender = VoiceProvider {
            transcriptions: AtomicUsize::new(0),
            interpretations: AtomicUsize::new(0),
            safety_screens: AtomicUsize::new(0),
            fragment_delay: Duration::ZERO,
            at: worker.sender.at.clone(),
            speech: command,
        };
        let worker = ModelDiaryWorker {
            sender: &sender,
            ..*worker
        };
        let wire = update(id, at, 10, "");
        let message_id = accept(binding, runtime, &wire, at).await?;
        let voice = database
            .claim_telegram_voice(target, clock(pool).await?, LEASE)
            .await?
            .ok_or("Expected recorded command before transcription")?;
        worker
            .process_voice(transport, &voice, clock(pool).await?)
            .await;
        assert_eq!(sender.transcriptions.load(Ordering::SeqCst), 1);

        if matches!(command, "/pause" | "/resume") {
            let control = database
                .claim_local_telegram_control(target, clock(pool).await?, LEASE)
                .await?
                .ok_or("Expected local control after transcription")?;
            assert_eq!(control.processing_envelope()?.text(), Some(command));
            assert_eq!(accept(binding, runtime, &wire, at).await?, message_id);
            if command == "/resume" {
                set_audio_consent(pool, false).await?;
            }
            let result = database
                .apply_telegram_owner_control(&control, 2, "UTC")
                .await;
            if command == "/pause" {
                assert!(result?.created);
            } else {
                assert!(matches!(
                    result,
                    Err(TelegramControlError::VoiceConsentChanged)
                ));
                database.finish_unavailable_telegram_voice(&control).await?;
                set_audio_consent(pool, true).await?;
            }
            let enabled: bool = sqlx::query_scalar(
                "SELECT (value->>'enabled')::bool FROM app_state WHERE key='proactive:enabled'",
            )
            .fetch_one(pool)
            .await?;
            assert!(!enabled);
        } else if command == "/history" {
            let history = database
                .claim_local_telegram_history(target, clock(pool).await?, LEASE)
                .await?
                .ok_or("Expected local history after transcription")?;
            assert!(
                database
                    .apply_local_telegram_diary(&history, "UTC")
                    .await?
                    .created
            );
        } else {
            let screen = database
                .claim_telegram_local_screen(target, clock(pool).await?, LEASE)
                .await?
                .ok_or("Expected submitted command to clear local screening")?;
            database
                .apply_local_telegram_text(
                    &screen,
                    LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
                    "en",
                )
                .await?;
            let ready = database
                .claim_telegram_model_text(target, clock(pool).await?, LEASE)
                .await?
                .ok_or("Expected local classification of unknown recorded command")?;
            worker.process(&ready, clock(pool).await?).await;
            let text: String = sqlx::query_scalar(
                "SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE inbound_message_id=$1",
            )
            .bind(message_id)
            .fetch_one(pool)
            .await?;
            assert_eq!(text, "Неизвестная команда. Доступные команды: /help.");
        }
        assert_eq!(sender.interpretations.load(Ordering::SeqCst), 0);
        assert_eq!(sender.safety_screens.load(Ordering::SeqCst), 0);
        let receipt: (String, i64) = sqlx::query_as(
            "SELECT i.status,count(o.id) FROM inbound_messages i
             LEFT JOIN outbox_messages o ON o.inbound_message_id=i.id WHERE i.id=$1 GROUP BY i.status",
        )
        .bind(message_id)
        .fetch_one(pool)
        .await?;
        assert_eq!(receipt, ("processed".into(), 1));
    }
    println!("Recorded pause, revoked resume, history and unknown command used local handlers");
    Ok(())
}

async fn screen_with_short_voice_lease(
    worker: &ModelDiaryWorker<'_, '_, VoiceProvider<'_>>,
    transport: &TelegramTransport,
    binding: TelegramBinding<'_>,
    runtime: &mut RuntimeLease,
    pool: &PgPool,
    at: StoredInstant,
) -> Result<(), Box<dyn Error>> {
    let database = worker.database;
    let target = TelegramInboundTarget {
        owner_id: binding.owner_id,
        instance_id: binding.instance_id,
    };
    let text = ["x", "y", "z", "w"]
        .map(|value| value.repeat(12_000))
        .concat();
    let sender = VoiceProvider {
        transcriptions: AtomicUsize::new(0),
        interpretations: AtomicUsize::new(0),
        safety_screens: AtomicUsize::new(0),
        fragment_delay: Duration::from_secs(2),
        at: worker.sender.at.clone(),
        speech: &text,
    };
    let worker = ModelDiaryWorker {
        sender: &sender,
        ..*worker
    };
    let wire = update(78010, at, 10, "");
    let message_id = accept(binding, runtime, &wire, at).await?;
    let voice = database
        .claim_telegram_voice(target, clock(pool).await?, LEASE)
        .await?
        .ok_or("Expected voice note for bounded screening")?;
    worker
        .process_voice(transport, &voice, clock(pool).await?)
        .await;
    let screen = database
        .claim_telegram_local_screen(target, clock(pool).await?, LEASE)
        .await?
        .ok_or("Expected initial local screen")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await?;
    let now = clock(pool).await?;
    let ready = database
        .claim_telegram_model_text(target, now, 7)
        .await?
        .ok_or("Expected long voice text with a short lease")?;
    worker.process(&ready, now).await;
    let screens = sender.safety_screens.load(Ordering::SeqCst);
    assert!(
        (1..4).contains(&screens),
        "Expected partial screening within the lease, got {screens}"
    );
    assert_eq!(sender.interpretations.load(Ordering::SeqCst), 0);
    let (state, text): (String, String) = sqlx::query_as(
        "SELECT i.status, o.intent#>>'{blocks,0,text}'
         FROM inbound_messages i JOIN outbox_messages o ON o.inbound_message_id=i.id WHERE i.id=$1",
    )
    .bind(message_id)
    .fetch_one(pool)
    .await?;
    assert_eq!(state, "processed");
    assert_eq!(text, template("en", "agent.oversized"));
    assert!(
        database
            .claim_telegram_model_text(target, clock(pool).await?, LEASE)
            .await?
            .is_none()
    );
    println!(
        "Several slow safety fragments shared the inbox deadline and produced one bounded reply"
    );
    Ok(())
}
