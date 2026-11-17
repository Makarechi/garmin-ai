use super::{bind, request, respond};
use crate::telegram_input::{CallbackResolution, TelegramBinding};
use crate::telegram_transport::TelegramLoopError;

use std::{env, error::Error, str::FromStr, time::Duration};

use ga_db::{
    Database,
    action_tokens::{ActionTokenContext, issue_action_token},
    channel_ingress::telegram_poll::TelegramPollError,
    jobs::RecordedJobStatus,
};
use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::timeout;
use uuid::Uuid;

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
    println!(
        "Verified native polling, backlog gate, callback time, independent acknowledgement and durable rate limit"
    );
    Ok(())
}
