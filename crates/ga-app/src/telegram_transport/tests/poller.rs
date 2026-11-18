use super::{bind, request, respond};
use crate::telegram_input::{CallbackResolution, TelegramBinding};
use crate::telegram_settings::TelegramIngressSettings;
use crate::telegram_transport::TelegramLoopError;

use std::{env, error::Error, str::FromStr, time::Duration};

use ga_db::{
    Database,
    action_tokens::{ActionTokenContext, issue_action_token},
    channel_ingress::telegram_poll::TelegramPollError,
    jobs::{
        RecordedJobStatus,
        telegram_ack::{TelegramAckTarget, enqueue_telegram_ack},
    },
};
use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::timeout;
use uuid::Uuid;

#[tokio::test]
#[ignore = "runs the live worker loops against a synthetic Telegram server and disposable TimescaleDB"]
async fn worker_receives_updates_and_acknowledges_them_under_the_runtime_lease()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let options =
        PgConnectOptions::from_str(&url.replacen("postgresql+psycopg://", "postgresql://", 1))?
            .disable_statement_logging();
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
    assert!(database.acquire_runtime_lease().await.is_err());
    let settings = TelegramIngressSettings::selected(Some("1234567890123"), None)?
        .ok_or("Expected Telegram integration")?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         SELECT gen_random_uuid(),id,'telegram','primary','1234567890123','fixture' FROM people",
    ).execute(&pool).await?;

    let (listener, transport) = bind().await?;
    let webhook = async {
        let (mut request_stream, _) = listener.accept().await?;
        assert_eq!(
            request(&mut request_stream).await?["endpoint"],
            "getWebhookInfo"
        );
        respond(
            &mut request_stream,
            "200 OK",
            &[],
            r#"{"ok":true,"result":{"url":"https://synthetic.invalid/telegram"}}"#,
        )
        .await?;
        assert!(
            timeout(Duration::from_millis(350), listener.accept())
                .await
                .is_err()
        );
        Ok::<_, Box<dyn Error>>(())
    };
    timeout(Duration::from_secs(3), async {
        tokio::select! {
            result = webhook => result,
            result = transport.run_receiver(&database, &mut runtime, &settings) =>
                Err(format!("Webhook receiver exited: {result:?}").into()),
        }
    })
    .await??;

    let chat = settings.external_owner_id();
    let message = json!({
        "message_id":76, "chat":{"type":"private","id":chat},
        "from":{"id":chat}, "text":"synthetic Telegram input"
    });
    let (listener, transport) = bind().await?;
    let (poll_started, wait_for_poll) = tokio::sync::oneshot::channel();
    let server = async {
        let (mut status, _) = listener.accept().await?;
        assert_eq!(request(&mut status).await?["endpoint"], "getWebhookInfo");
        respond(
            &mut status,
            "200 OK",
            &[],
            r#"{"ok":true,"result":{"url":""}}"#,
        )
        .await?;
        let (mut first, _) = listener.accept().await?;
        assert_eq!(request(&mut first).await?["endpoint"], "getUpdates");
        let mut unhandled = message.clone();
        unhandled["message_id"] = json!(75);
        unhandled["text"] = json!("/goals");
        let mut pause = message.clone();
        pause["message_id"] = json!(77);
        pause["text"] = json!("\u{00a0}/pause\u{2003}");
        let updates = json!({"ok":true,"result":[
            {"update_id":39,"message":unhandled},
            {"update_id":40,"message":message},
            {"update_id":41,"callback_query":{"id":"synthetic-worker-ack","data":"expired",
                                             "message":message,"from":{"id":chat}}},
            {"update_id":42,"message":pause}
        ]});
        respond(&mut first, "200 OK", &[], &updates.to_string()).await?;
        let (mut long_poll, _) = listener.accept().await?;
        let next = request(&mut long_poll).await?;
        assert_eq!(next["endpoint"], "getUpdates");
        assert_eq!(next["fields"]["offset"], 43);
        let mut policy = pool.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock_shared(72104621)")
            .execute(&mut *policy)
            .await?;
        poll_started
            .send(())
            .map_err(|_| "Missing callback worker")?;
        timeout(Duration::from_secs(2), async {
            loop {
                let deferred: bool = sqlx::query_scalar(
                    "SELECT EXISTS(
                        SELECT FROM jobs j JOIN inbound_messages i
                          ON i.id::text=j.payload->>'inbound_message_id'
                        WHERE i.external_event_id='42' AND i.status='pending'
                          AND j.status='pending' AND j.attempts=0 AND j.last_error='TelegramPolicyBusy'
                    )",
                ).fetch_one(&pool).await?;
                if deferred {
                    return Ok::<_, sqlx::Error>(());
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        }).await.map_err(|_| "Pause did not return to its queue while policy was locked")??;
        policy.rollback().await?;
        timeout(Duration::from_secs(2), async {
            let mut endpoints = Vec::new();
            for _ in 0..2 {
                let (mut outgoing, _) = listener.accept().await?;
                let wire = request(&mut outgoing).await?;
                let endpoint = wire["endpoint"].as_str().ok_or("Missing endpoint")?;
                let body = match endpoint {
                    "answerCallbackQuery" => {
                        assert_eq!(wire["fields"]["callback_query_id"], "synthetic-worker-ack");
                        json!({"ok": true, "result": true})
                    }
                    "sendMessage" => {
                        assert_eq!(wire["fields"]["chat_id"], chat);
                        assert_eq!(wire["fields"]["text"], "Вопросы отключены. Синхронизация продолжается.");
                        json!({"ok": true, "result": {"message_id":7,"chat":{"id":chat,"type":"private"}}})
                    }
                    _ => return Err(format!("Unexpected Telegram endpoint {endpoint}").into()),
                };
                endpoints.push(endpoint.to_owned());
                respond(&mut outgoing, "200 OK", &[], &body.to_string()).await?;
            }
            endpoints.sort();
            assert_eq!(endpoints, ["answerCallbackQuery", "sendMessage"]);
            Ok::<_, Box<dyn Error>>(())
        }).await??;
        timeout(Duration::from_secs(2), async {
            loop {
                let finished: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT FROM jobs WHERE kind='telegram_native_ack' AND status='done')
                       AND EXISTS(SELECT FROM outbox_messages WHERE state='provider_accepted')",
                ).fetch_one(&pool).await?;
                if finished {
                    return Ok::<_, sqlx::Error>(());
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        }).await??;
        let terminated: bool = sqlx::query_scalar(
            "SELECT pg_terminate_backend(pid) FROM pg_locks
             WHERE locktype='advisory' AND mode='ExclusiveLock' AND granted
               AND classid=0 AND objid=72104620 AND objsubid=1
               AND database=(SELECT oid FROM pg_database WHERE datname=current_database())",
        )
        .fetch_one(&pool)
        .await?;
        assert!(terminated);
        let mut remainder = Vec::new();
        use tokio::io::AsyncReadExt;
        timeout(
            Duration::from_millis(2200),
            long_poll.read_to_end(&mut remainder),
        )
        .await??;
        assert!(remainder.is_empty());
        Ok::<_, Box<dyn Error>>(())
    };
    let callback = async {
        wait_for_poll.await?;
        tokio::select! {
            result = transport.run_acknowledgements(&database, &settings) => result,
            result = transport.run_owner_controls(&database, &settings, 2, "Europe/Bratislava") => result,
            result = transport.run_replies(&database, &settings) => result,
        }
    };
    let receiver = transport.run_receiver(&database, &mut runtime, &settings);
    let completion = async {
        tokio::select! {
            result = receiver => {
                assert!(result.is_err());
                Ok::<_, Box<dyn Error>>(())
            }
            result = callback => result,
        }
    };
    timeout(Duration::from_secs(8), async {
        tokio::try_join!(server, completion)
    })
    .await??;
    let retained: Vec<(String, String)> = sqlx::query_as(
        "SELECT external_event_id,status FROM inbound_messages ORDER BY external_event_id",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        retained,
        [
            ("39".into(), "pending".into()),
            ("40".into(), "pending".into()),
            ("42".into(), "processed".into())
        ]
    );
    let paused: bool = sqlx::query_scalar(
        "SELECT value->'enabled'='false'::jsonb FROM app_state WHERE key='proactive:enabled'",
    )
    .fetch_one(&pool)
    .await?;
    assert!(paused);
    assert!(database.acquire_runtime_lease().await.is_ok());
    Ok(())
}

#[tokio::test]
#[ignore = "runs a local Telegram wire fixture against disposable pinned TimescaleDB"]
async fn drains_poll_backlog_and_acknowledges_without_waiting_for_the_next_poll()
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
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut lease = database.acquire_runtime_lease().await?;
    let binding = TelegramBinding {
        owner_id,
        instance_id: "primary",
        external_owner_id: 1234567890123,
    };
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary',$2,'fixture')",
    ).bind(owner_id).bind(binding.external_owner_id.to_string()).execute(&pool).await?;
    let message = json!({
        "message_id":76,"chat":{"type":"private","id":binding.external_owner_id},
        "from":{"id":binding.external_owner_id},"text":"synthetic button parent"
    });
    let update = json!({"update_id":20,"message":message});
    let now = OffsetDateTime::now_utc();
    let at =
        StoredInstant::from_unix_microseconds(i64::try_from(now.unix_timestamp_nanos() / 1_000)?)?;
    let normalized = binding
        .authenticate(&update)?
        .normalize(at, CallbackResolution::Unavailable)?;
    database.ingest_channel_message(&normalized).await?;
    let conversation = normalized.conversation_id();
    let expires = StoredInstant::from_unix_microseconds(at.unix_microseconds() + 120_000_000)?;
    let mut buttons = Vec::new();
    let mut txn = pool.begin().await?;
    for (id, label) in [(21, "synthetic-old"), (121, "synthetic-new")] {
        let action = issue_action_token(
            &mut txn,
            ActionTokenContext {
                owner_id,
                conversation_id: conversation,
                revision: 1,
            },
            label,
            at,
            expires,
        )
        .await?;
        let intent = OutboundIntent::normalize(json!({
            "owner_id":owner_id.to_string(),"conversation_id":conversation.to_string(),
            "channel_instance":{"channel":"telegram","instance_id":"primary"},
            "actions":[{"action_id":label,"label":"Confirm","token":action,
                "operation_id":Uuid::new_v4().to_string(),"expires_at":expires.to_string()}]
        }))?;
        sqlx::query(
            "INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,intent,dedup_key,state,attempts)
             VALUES(gen_random_uuid(),$1,$2,gen_random_uuid(),$3::jsonb,$4,'provider_accepted',0)",
        ).bind(owner_id).bind(conversation).bind(intent.document().to_string()).bind(label).execute(&mut *txn).await?;
        buttons.push(json!({
            "update_id":id,
            "callback_query":{"id":label,"data":action,"message":message,"from":{"id":binding.external_owner_id}},
            "_callback_time_known":true
        }));
    }
    sqlx::query("INSERT INTO app_state(key,value) VALUES('telegram:offset','{\"offset\":9999,\"received_at\":\"2020-01-01T00:00:00Z\"}')")
        .execute(&mut *txn).await?;
    txn.commit().await?;

    let (listener, transport) = bind().await?;
    let (notify_ack, wait_for_ack) = tokio::sync::oneshot::channel();
    let mut backlog = vec![buttons[0].clone()];
    backlog.extend((22..121).map(|id| json!({
        "update_id":id,"message":{"chat":{"type":"private","id":42},"from":{"id":42},"text":"foreign"}
    })));
    let server = async {
        let (mut first, _) = listener.accept().await?;
        assert!(request(&mut first).await?["fields"].get("offset").is_none());
        respond(
            &mut first,
            "200 OK",
            &[],
            &json!({"ok":true,"result":backlog}).to_string(),
        )
        .await?;
        let (mut poll, _) = listener.accept().await?;
        let request_poll = request(&mut poll).await?;
        assert_eq!(request_poll["endpoint"], "getUpdates");
        assert_eq!(request_poll["fields"]["offset"], 121);
        notify_ack
            .send(())
            .map_err(|_| "Missing parallel acknowledgement task")?;
        let (mut ack, _) = listener.accept().await?;
        let request_ack = request(&mut ack).await?;
        assert_eq!(request_ack["endpoint"], "answerCallbackQuery");
        assert_eq!(request_ack["fields"]["callback_query_id"], "synthetic-old");
        for lock in [72_104_621_i64, 72_104_627] {
            let can_change: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                .bind(lock)
                .fetch_one(&pool)
                .await?;
            assert!(!can_change);
        }
        respond(
            &mut ack,
            "429 Too Many Requests",
            &[("Retry-After", "3")],
            r#"{"ok":false,"parameters":{"retry_after":3}}"#,
        )
        .await?;
        respond(&mut poll, "200 OK", &[], r#"{"ok":true,"result":[]}"#).await?;
        let (mut current, _) = listener.accept().await?;
        assert_eq!(request(&mut current).await?["fields"]["offset"], 121);
        respond(
            &mut current,
            "200 OK",
            &[],
            &json!({"ok":true,"result":[buttons[1]]}).to_string(),
        )
        .await?;
        let (mut ack, _) = listener.accept().await?;
        assert_eq!(
            request(&mut ack).await?["fields"]["callback_query_id"],
            "synthetic-new"
        );
        respond(&mut ack, "400 Bad Request", &[], "{}").await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let client = async {
        let mut progress = Default::default();
        let first = transport
            .poll_once(&mut lease, binding, &mut progress)
            .await?;
        assert_eq!(first.updates, 100);
        assert!(!first.notification_ready);
        let ack = async {
            wait_for_ack.await?;
            assert_eq!(
                transport.acknowledge_once(&database, binding).await?,
                Some(RecordedJobStatus::Deferred)
            );
            Ok::<_, Box<dyn Error>>(())
        };
        let (second, old_ack) =
            tokio::join!(transport.poll_once(&mut lease, binding, &mut progress), ack);
        old_ack?;
        assert!(second?.notification_ready);
        assert_eq!(
            transport
                .poll_once(&mut lease, binding, &mut progress)
                .await?
                .updates,
            1
        );
        assert_eq!(
            transport.acknowledge_once(&database, binding).await?,
            Some(RecordedJobStatus::Completed)
        );
        Ok::<_, Box<dyn Error>>(())
    };
    let (server, client) = timeout(Duration::from_secs(10), async {
        tokio::join!(server, client)
    })
    .await?;
    server?;
    client?;
    let times: Vec<String> = sqlx::query_scalar(
        "SELECT envelope->>'time_precision' FROM inbound_messages WHERE kind='action'
         ORDER BY external_event_id::int",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(times, ["unknown", "second"]);
    let jobs: Vec<(String, String)> = sqlx::query_as(
        "SELECT payload->>'callback_id',status FROM jobs WHERE kind='telegram_native_ack'
         ORDER BY payload->>'callback_id'",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        jobs,
        [
            ("synthetic-new".into(), "done".into()),
            ("synthetic-old".into(), "pending".into())
        ]
    );
    let mut setup = pool.begin().await?;
    let target = TelegramAckTarget {
        owner_id,
        instance_id: "primary",
        confirmed_chat: Some(binding.external_owner_id),
    };
    assert!(enqueue_telegram_ack(&mut setup, target, 124, "synthetic-policy-overlap", at,).await?);
    setup.commit().await?;
    let (listener, transport) = bind().await?;
    let mut policy = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104621)")
        .execute(&mut *policy)
        .await?;
    assert_eq!(
        transport.acknowledge_once(&database, binding).await?,
        Some(RecordedJobStatus::Deferred)
    );
    let rescheduled: (String, i32, String) = sqlx::query_as(
        "SELECT status,attempts,last_error FROM jobs
         WHERE kind='telegram_native_ack' AND payload->>'callback_id'='synthetic-policy-overlap'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        rescheduled,
        ("pending".into(), 0, "TelegramPolicyBusy".into())
    );
    assert!(
        timeout(Duration::from_millis(40), listener.accept())
            .await
            .is_err()
    );
    policy.rollback().await?;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let server = async {
        let (mut stream, _) = listener.accept().await?;
        assert_eq!(
            request(&mut stream).await?["fields"]["callback_query_id"],
            "synthetic-policy-overlap"
        );
        respond(&mut stream, "200 OK", &[], r#"{"ok":true,"result":true}"#).await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let (server, retry) = timeout(Duration::from_secs(2), async {
        tokio::join!(server, transport.acknowledge_once(&database, binding))
    })
    .await?;
    server?;
    assert_eq!(retry?, Some(RecordedJobStatus::Completed));

    let previous = lease.owned_telegram_poll_cursor("primary", at).await?;
    assert_eq!(previous.offset, Some(122));
    let (listener, transport) = bind().await?;
    let late_delivery = json!({"update_id":122,"message":message});
    let server = async {
        let (mut poll, _) = listener.accept().await?;
        assert_eq!(request(&mut poll).await?["fields"]["offset"], 122);
        sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE owner_id=$1")
            .bind(owner_id)
            .execute(&pool)
            .await?;
        respond(
            &mut poll,
            "200 OK",
            &[],
            &json!({"ok":true,"result":[late_delivery]}).to_string(),
        )
        .await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let mut progress = Default::default();
    let (server, result) = timeout(Duration::from_secs(5), async {
        tokio::join!(
            server,
            transport.poll_once(&mut lease, binding, &mut progress)
        )
    })
    .await?;
    server?;
    assert!(matches!(
        result,
        Err(TelegramLoopError::Ingress(TelegramPollError::Pairing(_)))
    ));
    assert_eq!(
        lease.owned_telegram_poll_cursor("primary", at).await?,
        previous
    );
    let late_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM inbound_messages WHERE external_event_id='122')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(!late_exists);
    assert!(matches!(
        timeout(
            Duration::from_secs(2),
            transport.poll_once(&mut lease, binding, &mut progress)
        )
        .await?,
        Err(TelegramLoopError::Ingress(TelegramPollError::Pairing(_)))
    ));
    assert!(
        timeout(Duration::from_millis(20), listener.accept())
            .await
            .is_err()
    );
    sqlx::query(
        "UPDATE jobs SET run_at=now() WHERE kind='telegram_native_ack' AND status='pending'",
    )
    .execute(&pool)
    .await?;
    assert_eq!(
        transport.acknowledge_once(&database, binding).await?,
        Some(RecordedJobStatus::Failed),
    );
    let stale: Option<String> = sqlx::query_scalar(
        "SELECT last_error FROM jobs WHERE kind='telegram_native_ack' AND payload->>'callback_id'='synthetic-old'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(stale.as_deref(), Some("TelegramPairingChanged"));
    assert!(
        timeout(Duration::from_millis(50), listener.accept())
            .await
            .is_err()
    );
    println!(
        "Verified independent acknowledgement, durable rate limit and no callback for a replaced pairing"
    );
    Ok(())
}
