use std::time::Instant;

use ga_model::ModelUnavailability;
use tokio::io::AsyncWriteExt;

use super::*;
use crate::telegram_transport::{
    MAX_VOICE_BYTES,
    tests::{bind, request, respond},
};

const PRIMARY: &str = "synthetic-primary";
const FALLBACK: &str = "synthetic-fallback";
const TRANSCRIPT: &str = "synthetic voice note";
const FILE_PATH: &str = "voice/ceiling.oga";

struct CeilingProvider {
    attempts: AtomicUsize,
}

impl Sender for CeilingProvider {
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
        let audio = &body["input"][0]["content"][0];
        assert_eq!(audio["type"], "audio");
        assert_eq!(audio["mime_type"], "audio/ogg");
        let encoded = audio["data"]
            .as_str()
            .ok_or(ModelTransportError::InvalidConfiguration)?;
        assert_eq!(encoded.len(), 4 * MAX_VOICE_BYTES.div_ceil(3));
        let (padding, content) = encoded
            .as_bytes()
            .split_last()
            .ok_or(ModelTransportError::InvalidConfiguration)?;
        assert_eq!(*padding, b'=');
        assert!(content.iter().all(|&byte| byte == b'A'));
        match self.attempts.fetch_add(1, Ordering::SeqCst) {
            0 => {
                assert_eq!(body["model"], PRIMARY);
                tokio::time::sleep(Duration::from_millis(200)).await;
                Err(ModelTransportError::Unavailable {
                    reason: ModelUnavailability::Upstream,
                    retry_after: Duration::from_secs(60),
                })
            }
            1 => {
                assert_eq!(body["model"], FALLBACK);
                request
                    .decode_response(
                        json!({"status":"completed","steps":[{"type":"model_output","content":[
                            {"type":"text","text":json!({"text":TRANSCRIPT}).to_string()}]}]})
                        .to_string()
                        .as_bytes(),
                    )
                    .map_err(ModelTransportError::Response)
            }
            _ => panic!("Expected one primary request and one authorized fallback"),
        }
    }
}

#[tokio::test]
#[ignore = "measures the capped voice path on a migrated disposable database"]
async fn transcribes_maximum_audio_and_rejects_one_extra_byte() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
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
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    )
    .bind(owner_id)
    .execute(&pool)
    .await?;
    set_audio_consent(&pool, true).await?;
    let at = clock(&pool).await?;
    let granted_at =
        StoredInstant::try_from(OffsetDateTime::from(at) - time::Duration::minutes(1))?;
    let model = RuntimeModelSettings::resolve(
        |name| {
            Ok(match name {
                "GA_LLM_ENABLED" => Some("true".into()),
                "GA_LLM_CONSENT" => Some(
                    json!({"provider":"gemini","provider_instance_id":"model:gemini:primary",
                           "model":PRIMARY,"fallback_models":[FALLBACK],
                           "categories":["audio"],"granted_at":granted_at.to_string(),
                           "policy_revision":1})
                    .to_string(),
                ),
                "GA_GEMINI_API_KEY" => Some("synthetic-gemini".into()),
                "GA_GEMINI_MODEL" => Some(PRIMARY.into()),
                "GA_GEMINI_FALLBACK_ENABLED" => Some("true".into()),
                "GA_GEMINI_FALLBACK_MODELS" => Some(json!([FALLBACK]).to_string()),
                _ => None,
            })
        },
        |_| Ok(None),
    )?
    .ok_or("Missing fixture model")?;
    let configured = model.configured();
    let gateway = configured.gateway(database.clone(), false, at)?;
    let provider = CeilingProvider {
        attempts: AtomicUsize::new(0),
    };
    let worker = ModelDiaryWorker {
        database: &database,
        configured: &configured,
        gateway: &gateway,
        sender: &provider,
        timezone: "UTC",
        locale: "en",
    };
    let (listener, transport) = bind().await?;
    let binding = TelegramBinding {
        owner_id,
        instance_id: "primary",
        external_owner_id: 42,
    };
    let target = TelegramInboundTarget {
        owner_id,
        instance_id: "primary",
    };
    let mut oversized = update(78101, at, 600, "");
    oversized["message"]["voice"]["file_size"] = json!(MAX_VOICE_BYTES + 1);
    assert!(
        binding
            .accept_webhook_update(&database, &oversized, at)
            .await?
    );
    let lease = database
        .claim_telegram_voice(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected oversized voice lease")?;
    worker
        .process_voice(&transport, &lease, clock(&pool).await?)
        .await;
    let status: String = sqlx::query_scalar("SELECT status FROM inbound_messages WHERE id=$1")
        .bind(lease.message_id())
        .fetch_one(&pool)
        .await?;
    assert_eq!(status, "invalid");
    assert_eq!(provider.attempts.load(Ordering::SeqCst), 0);
    assert!(
        tokio::time::timeout(Duration::from_millis(50), listener.accept())
            .await
            .is_err()
    );

    let mut wire = update(78102, at, 600, "");
    wire["message"]["voice"]["file_size"] = json!(MAX_VOICE_BYTES);
    let started = Instant::now();
    assert!(binding.accept_webhook_update(&database, &wire, at).await?);
    let lease = database
        .claim_telegram_voice(target, clock(&pool).await?, LEASE)
        .await?
        .ok_or("Expected maximum voice lease")?;
    let server = async {
        let (mut metadata, _) = listener.accept().await?;
        let metadata_request = request(&mut metadata).await?;
        assert_eq!(metadata_request["endpoint"], "getFile");
        assert_eq!(metadata_request["fields"]["file_id"], "synthetic_voice");
        respond(
            &mut metadata,
            "200 OK",
            &[],
            &json!({"ok":true,"result":{"file_path":FILE_PATH,"file_size":MAX_VOICE_BYTES}})
                .to_string(),
        )
        .await?;
        let (mut file, _) = listener.accept().await?;
        let download = request(&mut file).await?;
        assert_eq!(download["method"], "GET");
        assert!(
            download["endpoint"]
                .as_str()
                .is_some_and(|path| path.ends_with(FILE_PATH))
        );
        file.write_all(
            format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {MAX_VOICE_BYTES}\r\nConnection: close\r\n\r\n"
            )
            .as_bytes(),
        )
        .await?;
        const BLOCK: &[u8; 16 * 1024] = &[0; 16 * 1024];
        for _ in 0..MAX_VOICE_BYTES / BLOCK.len() {
            file.write_all(BLOCK).await?;
        }
        Ok::<_, Box<dyn Error>>(())
    };
    let (server, _) = tokio::time::timeout(Duration::from_secs(100), async {
        tokio::join!(server, worker.process_voice(&transport, &lease, at))
    })
    .await?;
    server?;
    let persisted: (String, String) =
        sqlx::query_as("SELECT normalized_text,status FROM inbound_messages WHERE id=$1")
            .bind(lease.message_id())
            .fetch_one(&pool)
            .await?;
    assert_eq!(persisted, (TRANSCRIPT.into(), "pending".into()));
    assert_eq!(provider.attempts.load(Ordering::SeqCst), 2);
    assert!(binding.accept_webhook_update(&database, &wire, at).await?);
    let saved: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM inbound_messages WHERE channel='telegram' AND kind='voice'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(saved, 2);
    assert!(
        database
            .claim_telegram_voice(target, clock(&pool).await?, LEASE)
            .await?
            .is_none()
    );
    println!(
        "WORKLOAD_RESULT={}",
        json!({"audio_bytes_per_call":MAX_VOICE_BYTES,"duration_seconds_per_clip":600,
            "repetitions":1,"downloads":1,"provider_models":[PRIMARY,FALLBACK],
            "oversized_rejected_before_download":true,
            "transcript_sha256":ga_domain::sha256_hex(TRANSCRIPT.as_bytes()),
            "transcript_committed_once":true,"elapsed_seconds":started.elapsed().as_secs_f64()})
    );
    Ok(())
}
