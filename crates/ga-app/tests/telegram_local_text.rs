use std::{env, error::Error, fs, str::FromStr, time::Duration};

use ga_app::{
    telegram_settings::TelegramIngressSettings, telegram_transport::TelegramTransport,
    urgent_screen::obvious_urgent_symptoms,
};
use ga_db::{
    Database,
    jobs::telegram_inbound::{TelegramInboundState, TelegramInboundTarget},
    runtime_lease::RuntimeLease,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::{sleep, timeout};

use ga_app::telegram_input::TelegramBinding;

async fn clock(pool: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(pool)
        .await?;
    Ok(StoredInstant::try_from(at)?)
}

async fn inbound(
    runtime: &mut RuntimeLease,
    binding: TelegramBinding<'_>,
    pool: &PgPool,
    update_id: i64,
    body: Value,
) -> Result<(), Box<dyn Error>> {
    let now = clock(pool).await?;
    let mut update = json!({
        "update_id":update_id,
        "message":{"message_id":update_id,"from":{"id":42},
            "chat":{"id":42,"type":"private"},"date":OffsetDateTime::from(now).unix_timestamp()}
    });
    let extra = body.as_object().ok_or("Invalid fixture message")?;
    update["message"]
        .as_object_mut()
        .ok_or("Missing message")?
        .extend(extra.clone());
    assert!(
        binding
            .accept_polled_update(runtime, &update, now, true)
            .await?
            .inbound
            .is_some()
    );
    Ok(())
}

async fn reply(pool: &PgPool, id: i64) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT o.intent#>>'{blocks,0,text}' FROM outbox_messages o
         JOIN inbound_messages i ON i.id=o.inbound_message_id WHERE i.external_event_id=$1",
    )
    .bind(id.to_string())
    .fetch_optional(pool)
    .await
}

async fn await_reply(pool: &PgPool, id: i64) -> Result<String, sqlx::Error> {
    loop {
        if let Some(response) = reply(pool, id).await? {
            return Ok(response);
        }
        sleep(Duration::from_millis(20)).await;
    }
}

#[tokio::test]
#[ignore = "uses authenticated native ingress and a disposable TimescaleDB"]
async fn local_text_worker_screens_urgent_reports_before_older_diary_jobs()
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
        .ok_or("Expected a disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let settings =
        TelegramIngressSettings::selected(Some("42"), None)?.ok_or("Missing settings")?;
    let binding = settings.confirmed_binding(&database).await?;
    let target = TelegramInboundTarget {
        owner_id: binding.owner_id,
        instance_id: binding.instance_id,
    };
    let transport = TelegramTransport::new("100:synthetic-local-text")?;
    let expected: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_LOCAL_TEXT")?)?)?;
    let voice = json!({"file_id":"synthetic-audio-reference","duration":1,"mime_type":"audio/ogg","file_size":128});
    inbound(
        &mut runtime,
        binding,
        &pool,
        101,
        json!({"text":"/newtracker"}),
    )
    .await?;
    inbound(
        &mut runtime,
        binding,
        &pool,
        102,
        json!({"text":"Synthetic ordinary report"}),
    )
    .await?;
    inbound(
        &mut runtime,
        binding,
        &pool,
        103,
        json!({"voice":voice,"caption":"I can't breathe"}),
    )
    .await?;
    inbound(&mut runtime, binding, &pool, 104, json!({"voice":voice})).await?;

    let poll = async {
        assert_eq!(json!(await_reply(&pool, 103).await?), expected["emergency"]);
        assert!(reply(&pool, 102).await?.is_none());
        assert!(reply(&pool, 104).await?.is_none());
        let now = clock(&pool).await?;
        let earlier = database
            .claim_telegram_inbound(target, now, 60)
            .await?
            .ok_or("Missing older setup command")?;
        assert!(earlier.envelope_json().contains("/newtracker"));
        database
            .finish_telegram_inbound(&earlier, now, TelegramInboundState::Processed)
            .await?;
        assert_eq!(json!(await_reply(&pool, 102).await?), expected["text"]);
        assert_eq!(json!(await_reply(&pool, 104).await?), expected["voice"]);

        sqlx::query(
            "INSERT INTO app_state(key,value) VALUES('tracker:chat-setup:telegram:primary',$1::jsonb)",
        ).bind(json!({"locale":"en","name":"Synthetic draft","step":"fields"}).to_string())
         .execute(&pool).await?;
        let label = "Sudden severe chest pain | text";
        assert!(obvious_urgent_symptoms(label)?);
        inbound(&mut runtime, binding, &pool, 105, json!({"text":label})).await?;
        inbound(
            &mut runtime,
            binding,
            &pool,
            106,
            json!({"text":"I can't breathe"}),
        )
        .await?;
        assert_eq!(json!(await_reply(&pool, 106).await?), expected["emergency"]);
        assert!(reply(&pool, 105).await?.is_none());
        let diary_writes: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
            .fetch_one(&pool)
            .await?;
        assert_eq!(diary_writes, 0);
        Ok::<_, Box<dyn Error>>(())
    };
    timeout(Duration::from_secs(8), async {
        tokio::select! {
            result = poll => result,
            worker = transport.run_local_text(&database, &settings, "en", false) => worker,
        }
    })
    .await??;

    let wait_for_screen = async {
        sqlx::query("DELETE FROM app_state WHERE key='tracker:chat-setup:telegram:primary'")
            .execute(&pool)
            .await?;
        let older = loop {
            if let Some(lease) = database
                .claim_telegram_inbound(target, clock(&pool).await?, 60)
                .await?
            {
                break lease;
            }
            sleep(Duration::from_millis(50)).await;
        };
        assert!(
            older
                .envelope_json()
                .contains("Sudden severe chest pain | text")
        );
        database
            .finish_telegram_inbound(&older, clock(&pool).await?, TelegramInboundState::Processed)
            .await?;
        inbound(
            &mut runtime,
            binding,
            &pool,
            107,
            json!({"text":"Synthetic report awaiting interpretation"}),
        )
        .await?;
        loop {
            let safe_pending: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM jobs j JOIN inbound_messages i
                   ON j.payload->>'inbound_message_id'=i.id::text
                   WHERE i.external_event_id='107' AND i.status='pending' AND j.status='pending'
                     AND j.payload->>'local_safety_checked'='true')",
            )
            .fetch_one(&pool)
            .await?;
            if safe_pending {
                break;
            }
            sleep(Duration::from_millis(20)).await;
        }
        assert!(reply(&pool, 107).await?.is_none());
        Ok::<_, Box<dyn Error>>(())
    };
    timeout(Duration::from_secs(15), async {
        tokio::select! {
            result = wait_for_screen => result,
            worker = transport.run_local_text(&database, &settings, "en", true) => worker,
        }
    })
    .await??;
    Ok(())
}
