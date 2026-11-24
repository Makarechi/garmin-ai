use std::{env, error::Error, str::FromStr, time::Duration};

use ga_db::{
    Database,
    channel_ingress::telegram_poll::TelegramPollError,
    jobs::{JobStoreError, telegram_inbound::TelegramInboundTarget},
};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::{Instant, sleep, timeout};
use uuid::Uuid;

use super::{bind, request, respond};
use crate::telegram_settings::TelegramIngressSettings;
use crate::telegram_transport::poller::wall_clock;

async fn transactions(pool: &PgPool) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT xact_commit + xact_rollback FROM pg_stat_database
         WHERE datname=current_database()",
    )
    .fetch_one(pool)
    .await
}

#[tokio::test]
#[ignore = "measures idle consumers and cross-connection wakeups on isolated TimescaleDB"]
async fn idle_consumers_wait_and_webhook_work_wakes_control_and_delivery()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let options =
        PgConnectOptions::from_str(&url.replacen("postgresql+psycopg://", "postgresql://", 1))?
            .disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected a dedicated disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    // A separate Database models the webhook API process: local subscriptions
    // cannot carry its writes to the worker.
    let api = Database::connect_lazy(&url).map_err(|_| "Invalid API fixture connection")?;
    let _runtime = database.acquire_runtime_lease().await?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,
             confirmation_method,confirmed_at)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture',clock_timestamp())",
    )
    .bind(owner)
    .execute(&pool)
    .await?;
    let settings =
        TelegramIngressSettings::selected(Some("42"), None)?.ok_or("Missing settings")?;
    let binding = settings.confirmed_binding(&api).await?;
    let mut held_ingress = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104623)")
        .execute(&mut *held_ingress)
        .await?;
    assert!(matches!(
        database
            .claim_local_telegram_control(
                TelegramInboundTarget {
                    owner_id: binding.owner_id,
                    instance_id: binding.instance_id,
                },
                wall_clock()?,
                5,
            )
            .await,
        Err(JobStoreError::Busy)
    ));
    held_ingress.rollback().await?;
    let (listener, transport) = bind().await?;
    let proof = async {
        // Let startup/reconnect reconciliation finish before observing a quiet
        // interval. PostgreSQL reports backend statistics asynchronously.
        sleep(Duration::from_secs(3)).await;
        let before = transactions(&pool).await?;
        let idle_started = Instant::now();
        sleep(Duration::from_secs(5)).await;
        let idle_transactions = transactions(&pool).await? - before;
        eprintln!(
            "Telegram idle consumers: {idle_transactions} transactions in {:.3}s",
            idle_started.elapsed().as_secs_f64()
        );
        assert!(
            idle_transactions < 60,
            "Empty consumers performed {idle_transactions} transactions in five seconds"
        );

        let update = json!({"update_id":701,"message":{
            "message_id":701,"from":{"id":42},"chat":{"id":42,"type":"private"},
            "text":"/pause","date":OffsetDateTime::now_utc().unix_timestamp()
        }});
        timeout(Duration::from_secs(3), async {
            let received_at = Instant::now();
            loop {
                let now = wall_clock()?;
                match binding.accept_webhook_update(&api, &update, now).await {
                    Err(TelegramPollError::Busy) => sleep(Duration::from_millis(5)).await,
                    result => {
                        assert!(result?);
                        break;
                    }
                }
            }
            let (mut outgoing, _) = listener.accept().await?;
            let wire = request(&mut outgoing).await?;
            eprintln!(
                "Telegram webhook to sendMessage: {:.3}s",
                received_at.elapsed().as_secs_f64()
            );
            assert_eq!(wire["endpoint"], "sendMessage");
            assert_eq!(wire["fields"]["chat_id"], 42);
            assert!(
                wire["fields"]["text"]
                    .as_str()
                    .is_some_and(|text| !text.is_empty())
            );
            respond(
                &mut outgoing,
                "200 OK",
                &[],
                r#"{"ok":true,"result":{"message_id":702,"chat":{"id":42,"type":"private"}}}"#,
            )
            .await?;
            loop {
                let sent: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM outbox_messages WHERE state='provider_accepted')",
                )
                .fetch_one(&pool)
                .await?;
                if sent {
                    break;
                }
                sleep(Duration::from_millis(10)).await;
            }
            Ok::<_, Box<dyn Error>>(())
        })
        .await??;
        Ok::<_, Box<dyn Error>>(())
    };
    Box::pin(timeout(Duration::from_secs(15), async {
        tokio::select! {
            result = proof => result,
            worker = database.run_work_wakeups() => worker,
            worker = transport.run_acknowledgements(&database, &settings) => worker,
            worker = transport.run_owner_controls(&database, &settings, 2, "UTC") => worker,
            worker = transport.run_local_diary(&database, &settings, "UTC") => worker,
            worker = transport.run_tracker_setup(&database, &settings, "UTC", "en", true) => worker,
            worker = transport.run_tracker_forms(&database, &settings, "UTC", "en", true) => worker,
            worker = transport.run_local_text(&database, &settings, "en", true) => worker,
            worker = transport.run_replies(&database, &settings) => worker,
        }
    }))
    .await??;
    Ok(())
}
