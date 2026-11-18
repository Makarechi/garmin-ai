use std::{env, error::Error, str::FromStr, time::Duration};

use ga_archive::backup_space::{BackupSpace, Volume, VolumeRole};
use ga_db::{
    Database,
    outbox_claims::{ReplyDestination, ReplyDispatchResult},
    runtime_lease::RuntimeWorkerIdentity,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::{sleep, timeout};
use uuid::Uuid;

use super::{bind, request, respond};
use crate::telegram_settings::TelegramIngressSettings;

async fn capacity(
    database: &Database,
    worker: RuntimeWorkerIdentity,
    at: OffsetDateTime,
    offset: i64,
    ready: bool,
) -> Result<(), Box<dyn Error>> {
    let observation = StoredInstant::try_from(at)?;
    let bucket_time = StoredInstant::try_from(at - time::Duration::hours(offset))?;
    database.schedule_storage_check(bucket_time).await?;
    let lease = database
        .claim_storage_check(observation, 60)
        .await?
        .ok_or("Expected a due capacity check")?;
    database
        .complete_storage_check(
            &lease,
            BackupSpace {
                volumes: vec![Volume {
                    role: VolumeRole::Shared,
                    free_bytes: if ready { 200 } else { 10 },
                    required_bytes: 100,
                }],
            },
            observation,
            worker,
            true,
        )
        .await?;
    Ok(())
}

#[tokio::test]
#[ignore = "checks scheduled warnings on isolated TimescaleDB and a local Telegram wire fixture"]
async fn warns_about_storage_only_while_current_and_preserves_the_receipt()
-> Result<(), Box<dyn Error>> {
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
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let worker = runtime.worker_identity().await?;
    let settings =
        TelegramIngressSettings::selected(Some("42"), None)?.ok_or("Missing settings")?;
    let chat = settings.external_owner_id();
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(at)?;
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "telegram",
        instance_id: "primary",
    };
    let yesterday = at
        .date()
        .previous_day()
        .ok_or("Missing yesterday")?
        .to_string();
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES(gen_random_uuid(),'telegram_storage_notice',$1::jsonb,$2,'pending',now(),0)",
    )
    .bind(json!({"day":yesterday}).to_string())
    .bind(format!("storage-notice:{yesterday}"))
    .execute(&pool)
    .await?;
    let receipt = format!("outbox:storage-notice:{}:0", at.date());
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,'{\"status\":\"sent\",\"message_id\":19}')",
    )
    .bind(&receipt)
    .execute(&pool)
    .await?;
    capacity(&database, worker, at, 0, false).await?;
    database
        .queue_telegram_storage_notice(owner, "primary", chat, now)
        .await?;
    assert!(
        database
            .claim_outbox_reply(destination(), now, 60)
            .await?
            .is_none()
    );
    sqlx::query("DELETE FROM app_state WHERE key=$1")
        .bind(&receipt)
        .execute(&pool)
        .await?;

    database
        .queue_telegram_storage_notice(owner, "primary", chat, now)
        .await?;
    let claim = database
        .claim_outbox_reply(destination(), now, 60)
        .await?
        .ok_or("No warning")?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES('preferences:onboarding',$1::jsonb)")
        .bind(
            json!({"channel":{"channel":"web","instance_id":"local"},
            "fallback_channels":[{"channel":"telegram","instance_id":"primary"}]})
            .to_string(),
        )
        .execute(&pool)
        .await?;
    let fallback = database
        .dispatch_telegram_outbox_reply(destination(), chat, claim, &[], |_| async {
            panic!("A fallback channel cannot receive a storage notice")
        })
        .await?;
    assert_eq!(fallback, ReplyDispatchResult::Cancelled);
    database
        .queue_telegram_storage_notice(owner, "primary", chat, now)
        .await?;
    assert!(
        database
            .claim_outbox_reply(destination(), now, 60)
            .await?
            .is_none()
    );
    sqlx::query("DELETE FROM app_state WHERE key='preferences:onboarding'")
        .execute(&pool)
        .await?;
    database
        .queue_telegram_storage_notice(owner, "primary", chat, now)
        .await?;
    let claim = database
        .claim_outbox_reply(destination(), now, 60)
        .await?
        .ok_or("No warning after restoring the primary channel")?;
    capacity(&database, worker, at, 6, true).await?;
    let silent = database
        .dispatch_telegram_outbox_reply(destination(), chat, claim, &[], |_| async {
            panic!("A resolved storage shortage must not reach Telegram")
        })
        .await?;
    assert_eq!(silent, ReplyDispatchResult::Cancelled);
    capacity(&database, worker, at, 12, false).await?;

    let (listener, transport) = bind().await?;
    let server = async {
        for attempt in 0..2 {
            let (mut stream, _) = listener.accept().await?;
            let wire = request(&mut stream).await?;
            assert_eq!(wire["endpoint"], "sendMessage");
            assert_eq!(wire["fields"]["chat_id"], chat);
            assert_eq!(
                wire["fields"]["text"],
                "Мало свободного места для следующей резервной копии. Проверьте хранилище и запустите backup-space. Последняя копия не удалялась автоматически."
            );
            assert!(wire["fields"].get("reply_markup").is_none());
            let status: String =
                sqlx::query_scalar("SELECT value->>'status' FROM app_state WHERE key=$1")
                    .bind(&receipt)
                    .fetch_one(&pool)
                    .await?;
            assert_eq!(status, "sending");
            if attempt == 0 {
                respond(
                    &mut stream,
                    "429 Too Many Requests",
                    &[],
                    r#"{"ok":false,"error_code":429,"parameters":{"retry_after":1}}"#,
                )
                .await?;
            } else {
                respond(
                    &mut stream,
                    "200 OK",
                    &[],
                    &json!({"ok":true,"result":{
                        "message_id":91,"chat":{"id":chat,"type":"private"},
                    }})
                    .to_string(),
                )
                .await?;
            }
        }
        loop {
            let sent: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM outbox_messages WHERE state='provider_accepted')
                        AND EXISTS(SELECT 1 FROM jobs WHERE kind='telegram_storage_notice' AND status='done')
                        AND EXISTS(SELECT 1 FROM app_state WHERE key=$1
                          AND value->>'status'='sent' AND value->'message_id'='91'::jsonb)",
            ).bind(&receipt).fetch_one(&pool).await?;
            if sent {
                break;
            }
            sleep(Duration::from_millis(20)).await;
        }
        assert!(
            timeout(Duration::from_millis(350), listener.accept())
                .await
                .is_err()
        );
        Ok::<_, Box<dyn Error>>(())
    };
    timeout(Duration::from_secs(6), async {
        tokio::select! {
            completion = server => completion,
            worker = transport.run_replies(&database, &settings) => worker,
        }
    })
    .await??;
    database
        .queue_telegram_storage_notice(owner, "primary", chat, now)
        .await?;
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM outbox_messages")
        .fetch_one(&pool)
        .await?;
    assert_eq!(count, 1);
    Ok(())
}
