use std::{env, error::Error, fs, str::FromStr, time::Duration};

use ga_app::{telegram_settings::TelegramIngressSettings, telegram_transport::TelegramTransport};
use ga_db::{
    Database,
    jobs::telegram_inbound::{
        LocalTelegramText, TelegramDiaryCommitError, TelegramInboundLease, TelegramInboundTarget,
        TelegramModelAdmission, TelegramSafetyDecision,
    },
    runtime_lease::RuntimeLease,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::{sleep, timeout};

const KEY: &str = "tracker:chat-setup:telegram:primary";

async fn clock(pool: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::try_from(
        sqlx::query_scalar::<_, OffsetDateTime>("SELECT clock_timestamp()")
            .fetch_one(pool)
            .await?,
    )?)
}

#[path = "support/telegram_ingress.rs"]
mod telegram_ingress;
use telegram_ingress::receive as accept;

async fn before(pool: &PgPool, case: &Value) -> Result<(), Box<dyn Error>> {
    if case["pending"] == true {
        sqlx::query("INSERT INTO app_state(key,value) VALUES('conversation:pending',
            jsonb_build_object('created_at',clock_timestamp(),'channel_instance_id','telegram:primary','button','note'))")
            .execute(pool).await?;
    }
    if case["patch"].is_object() {
        let changed = sqlx::query("UPDATE app_state SET value=value||$2::jsonb WHERE key=$1")
            .bind(KEY)
            .bind(case["patch"].to_string())
            .execute(pool)
            .await?;
        assert_eq!(changed.rows_affected(), 1);
    }
    if let Some(delta) = case["activity_delta"].as_i64() {
        sqlx::query(
            "UPDATE app_state SET value=jsonb_set(value,'{last_activity_at}',
            to_jsonb(clock_timestamp()+$2::bigint*interval '1 second')) WHERE key=$1",
        )
        .bind(KEY)
        .bind(delta)
        .execute(pool)
        .await?;
    }
    if case["expire_preview"] == true {
        sqlx::query(
            "UPDATE app_state SET value=jsonb_set(value,'{expires_at}',
            to_jsonb('2000-01-01T00:00:00+00:00'::text)) WHERE key LIKE 'tracker-preview:%'",
        )
        .execute(pool)
        .await?;
    }
    Ok(())
}

fn normalized(mut draft: Value) -> Result<Value, Box<dyn Error>> {
    let Some(fields) = draft.as_object_mut() else {
        assert!(draft.is_null());
        return Ok(draft);
    };
    for key in [
        "started_at",
        "last_activity_at",
        "last_prompt_advanced_at",
        "preview_issued_at",
    ] {
        if let Some(value) = fields.remove(key) {
            assert!(StoredInstant::from_legacy_input(&value).is_some(), "{key}");
        }
    }
    let key = fields
        .get("key")
        .and_then(Value::as_str)
        .ok_or("Missing draft key")?;
    if key.strip_prefix("chat_").is_some_and(|suffix| {
        suffix.len() == 16 && suffix.bytes().all(|byte| byte.is_ascii_hexdigit())
    }) {
        fields.insert("key".to_owned(), json!("<generated>"));
    }
    if let Some(token) = fields
        .get("confirmation_token")
        .filter(|value| !value.is_null())
    {
        let token = token.as_str().ok_or("Invalid preview token")?;
        assert!(token.len() == 64 && token.bytes().all(|byte| byte.is_ascii_hexdigit()));
        fields.insert("confirmation_token".to_owned(), json!("<token>"));
    }
    Ok(draft)
}

async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let draft: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(KEY)
            .fetch_optional(pool)
            .await?;
    let definitions: String = sqlx::query_scalar("SELECT coalesce(jsonb_agg(jsonb_build_array(key,status,revision,current_version) ORDER BY key),'[]')::text
        FROM event_definitions WHERE namespace='user'").fetch_one(pool).await?;
    let trackers: String = sqlx::query_scalar("SELECT coalesce(jsonb_agg(jsonb_build_array(d.key,t.shortcut,t.reminder_enabled,t.reminder_time,
        t.reminder_timezone,t.revision,t.settings) ORDER BY d.key),'[]')::text
        FROM tracker_configs t JOIN event_definitions d ON d.id=t.definition_id").fetch_one(pool).await?;
    Ok(
        json!({"draft":normalized(draft.map(|raw|serde_json::from_str(&raw)).transpose()?.unwrap_or(Value::Null))?,
        "definitions":serde_json::from_str::<Value>(&definitions)?,"trackers":serde_json::from_str::<Value>(&trackers)?}),
    )
}

async fn exercise(
    database: &Database,
    runtime: &mut RuntimeLease,
    settings: &TelegramIngressSettings,
    pool: &PgPool,
    cases: &[Value],
) -> Result<(), Box<dyn Error>> {
    let binding = settings.confirmed_binding(database).await?;
    for case in cases {
        before(pool, case).await?;
        let now = clock(pool).await?;
        let id = case["update"].as_i64().ok_or("Missing update")?;
        let mut message = json!({"message_id":id,"from":{"id":42},"chat":{"id":42,"type":"private"},
            "date":OffsetDateTime::from(now).unix_timestamp()+case["sent_delta"].as_i64().unwrap_or(0)});
        message.as_object_mut().ok_or("Expected message")?.extend(
            case.get("message")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default(),
        );
        let wire = if let Some(data) = case["callback"].as_str() {
            json!({"update_id":id,"callback_query":{
                "id":format!("synthetic-setup-callback-{id}"),"from":{"id":42},
                "message":message,"data":data}})
        } else {
            json!({"update_id":id,"message":message})
        };
        let received = accept(runtime, binding, &wire, now, true).await?;
        assert!(received.inbound.is_some(), "{id}");
        let response = timeout(Duration::from_secs(8),async {
            loop {
                let reply: Option<String> = sqlx::query_scalar("SELECT o.intent#>>'{blocks,0,text}' FROM outbox_messages o
                    JOIN inbound_messages i ON i.id=o.inbound_message_id WHERE i.external_event_id=$1")
                    .bind(id.to_string()).fetch_optional(pool).await?;
                if let Some(reply) = reply { return Ok::<_,sqlx::Error>(reply); }
                sleep(Duration::from_millis(15)).await;
            }
        }).await.map_err(|_|format!("Tracker setup step {id} has no durable reply"))??;
        assert_eq!(
            json!(response),
            case["reply"],
            "reply {id}, {}",
            case["message"]
        );
        assert_eq!(
            snapshot(pool).await?,
            case["snapshot"],
            "state {id}, {}",
            case["message"]
        );
        let processed: bool = sqlx::query_scalar("SELECT i.status='processed' AND j.status='done'
            FROM inbound_messages i JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id)
            WHERE i.external_event_id=$1").bind(id.to_string()).fetch_one(pool).await?;
        assert!(processed, "Atomic inbox completion {id}");
        let repeated = accept(runtime, binding, &wire, clock(pool).await?, true).await?;
        assert!(
            repeated.inbound.is_some_and(|row| !row.created),
            "Duplicate update {id}"
        );
    }
    println!("Matched durable tracker setup steps {}", cases.len());
    let consents: i64 =
        sqlx::query_scalar("SELECT count(*) FROM app_state WHERE key LIKE 'tracker-consent:%'")
            .fetch_one(pool)
            .await?;
    assert_eq!(consents, 0);
    Ok(())
}

async fn admission(
    database: &Database,
    lease: &TelegramInboundLease,
) -> Result<TelegramModelAdmission, TelegramDiaryCommitError> {
    timeout(Duration::from_secs(3), async {
        loop {
            match database.admit_telegram_model_text(lease, "UTC", "en").await {
                Err(TelegramDiaryCommitError::Outbox(
                    ga_db::outbox_enqueue::QueueIntentError::Busy,
                )) => {
                    sleep(Duration::from_millis(5)).await;
                }
                result => return result,
            }
        }
    })
    .await
    .unwrap_or(Err(TelegramDiaryCommitError::Database))
}

async fn resume_local_setup_before_model(
    url: &str,
    runtime: &mut RuntimeLease,
    settings: &TelegramIngressSettings,
    pool: &PgPool,
    cases: &[Value],
) -> Result<(), Box<dyn Error>> {
    let database = Database::connect_lazy(url).map_err(|_| "Invalid recovery fixture URL")?;
    let binding = settings.confirmed_binding(&database).await?;
    let target = TelegramInboundTarget {
        owner_id: binding.owner_id,
        instance_id: binding.instance_id,
    };
    let at = StoredInstant::try_from(
        OffsetDateTime::from(clock(pool).await?) - time::Duration::seconds(1),
    )?;
    let state = json!({"key":"chat_model_recovery","name":null,"fields":[],"locale":"en",
        "timezone":"UTC","privacy":"private","confirmation_token":null,"started_at":at.to_string(),
        "last_activity_at":at.to_string(),"last_prompt_advanced_at":at.to_string(),
        "started_provider_update_id":60000,"last_provider_update_id":60000,
        "started_update_id":60000,"last_update_id":60000,"started_ordering_epoch":0,"last_ordering_epoch":0});
    sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
        .bind(KEY)
        .bind(state.to_string())
        .execute(pool)
        .await?;
    let now = clock(pool).await?;
    accept(runtime,binding,&json!({"update_id":60101,"message":{"message_id":60101,
        "from":{"id":42},"chat":{"id":42,"type":"private"},
        "date":OffsetDateTime::from(now).unix_timestamp(),"text":"Synthetic recovered model draft"}}),now,true).await?;
    let screen = database
        .claim_telegram_local_screen(target, clock(pool).await?, 10)
        .await?
        .ok_or("Missing local screening lease")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await?;
    let abandoned = database
        .claim_telegram_model_text(target, clock(pool).await?, 10)
        .await?
        .ok_or("Missing model admission lease")?;
    sqlx::query("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1 AND lease_token=$2")
        .bind(abandoned.job_identity().0).bind(abandoned.job_identity().1).execute(pool).await?;
    let recovered = database
        .claim_telegram_model_text(target, clock(pool).await?, 10)
        .await?
        .ok_or("Missing recovered model admission lease")?;
    assert!(matches!(
        admission(&database, &abandoned).await,
        Err(TelegramDiaryCommitError::Job(
            ga_db::jobs::JobStoreError::LeaseLost
        ))
    ));
    assert_eq!(snapshot(pool).await?["draft"]["name"], Value::Null);
    assert!(matches!(
        admission(&database, &recovered).await?,
        TelegramModelAdmission::Replied
    ));
    assert_eq!(
        snapshot(pool).await?["draft"]["name"],
        "Synthetic recovered model draft"
    );
    let expected = cases
        .iter()
        .find(|case| case["message"]["text"] == "Synthetic [focus]_tracker!")
        .ok_or("Missing field-help reference")?;
    let replies: Vec<String> = sqlx::query_scalar(
        "SELECT o.intent#>>'{blocks,0,text}' FROM outbox_messages o
        JOIN inbound_messages i ON i.id=o.inbound_message_id WHERE i.external_event_id='60101'",
    )
    .fetch_all(pool)
    .await?;
    assert_eq!(json!(replies), json!([expected["reply"]]));
    assert!(matches!(
        admission(&database, &recovered).await,
        Err(TelegramDiaryCommitError::Job(
            ga_db::jobs::JobStoreError::LeaseLost
        ))
    ));
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
        .fetch_one(pool)
        .await?;
    assert_eq!(count, 0);
    println!("Recovered setup admission after lease expiry and rejected both stale retries");
    Ok(())
}

#[tokio::test]
#[ignore = "compares local setup workers with frozen Python in a disposable TimescaleDB"]
async fn local_tracker_setup_matches_frozen_conversation() -> Result<(), Box<dyn Error>> {
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
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid test URL")?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let settings =
        TelegramIngressSettings::selected(Some("42"), None)?.ok_or("Missing settings")?;
    let transport = TelegramTransport::new("100:synthetic-tracker-chat-setup")?;
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TRACKER_CHAT_SETUP")?)?)?;
    tokio::select! {
        result=exercise(&database,&mut runtime,&settings,&pool,&cases)=>result?,
        worker=database.run_work_wakeups()=>worker,
        worker=transport.run_tracker_setup(&database,&settings,"UTC","en",false)=>worker,
        worker=transport.run_local_text(&database,&settings,"en",false)=>worker,
        worker=transport.run_local_diary(&database,&settings,"UTC")=>worker,
    }
    resume_local_setup_before_model(&url, &mut runtime, &settings, &pool, &cases).await?;
    if let Ok(status) = fs::read_to_string("/proc/self/status") {
        for line in status
            .lines()
            .filter(|line| line.starts_with("VmHWM:") || line.starts_with("VmRSS:"))
        {
            eprintln!("{line}");
        }
        for name in ["memory.peak", "memory.swap.peak", "memory.events"] {
            eprintln!(
                "{name}\n{}",
                fs::read_to_string(format!("/sys/fs/cgroup/{name}"))?
            );
        }
    }
    pool.close().await;
    Ok(())
}
