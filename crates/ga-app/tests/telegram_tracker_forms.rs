use std::{
    collections::BTreeSet,
    env,
    error::Error,
    fs,
    str::FromStr,
    sync::atomic::{AtomicUsize, Ordering},
    time::Duration,
};

use ga_app::{
    telegram_input::TelegramBinding, telegram_settings::TelegramIngressSettings,
    telegram_transport::TelegramTransport,
};
use ga_db::{
    Database,
    jobs::telegram_inbound::{
        LocalTelegramText, TelegramDiaryCommitError, TelegramInboundLease, TelegramInboundTarget,
        TelegramModelAdmission, TelegramSafetyDecision,
    },
    share_policy::{
        DestinationKind, ShareCategory, ShareDestination, SharePolicyError, TrackerShareGrant,
    },
};
use ga_domain::{
    access::{Scope, Scopes},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::{sleep, timeout};
use uuid::Uuid;

const PROVIDER_UNAVAILABLE_REPLY: &str = "Обработка свободного текста пока недоступна. Записи можно добавить кнопками, показатели посмотреть через /today.";

async fn clock(pool: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::try_from(
        sqlx::query_scalar::<_, OffsetDateTime>("SELECT clock_timestamp()")
            .fetch_one(pool)
            .await?,
    )?)
}

#[path = "support/telegram_ingress.rs"]
mod telegram_ingress;
use telegram_ingress::receive;

#[path = "support/tracker_snapshot.rs"]
mod tracker_snapshot;
async fn snapshot(pool: &PgPool, cases: &[Value]) -> Result<Value, Box<dyn Error>> {
    if cases
        .iter()
        .any(|case| case["snapshot"]["nl_state"].is_object())
    {
        tracker_snapshot::with_nl_state(pool, cases).await
    } else {
        tracker_snapshot::snapshot(pool).await
    }
}

async fn wire(
    pool: &PgPool,
    database: &Database,
    case: &Value,
    id: i64,
    at: StoredInstant,
) -> Result<Value, Box<dyn Error>> {
    if let Some(name) = case["legacy_tracker"].as_str() {
        let version: Uuid = sqlx::query_scalar(
            "SELECT v.id FROM event_definitions d JOIN event_definition_versions v
             ON v.definition_id=d.id AND v.version=d.current_version WHERE d.key=$1",
        )
        .bind(format!("user.synthetic_chat_{name}"))
        .fetch_one(pool)
        .await?;
        let pending = json!({
            "definition_version_id":version.to_string(), "button":"tracker_form",
            "action":"log", "event_ids":[], "question":"Synthetic legacy tracker prompt",
            "channel_instance_id":"telegram:primary", "created_at":at.to_string()
        });
        sqlx::query(
            "INSERT INTO app_state(key,value) VALUES('conversation:pending',$1::jsonb)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        )
        .bind(pending.to_string())
        .execute(pool)
        .await?;
    }
    if let Some(states) = case["states"].as_object() {
        for (key, value) in states {
            sqlx::query("DELETE FROM app_state WHERE key=$1")
                .bind(key)
                .execute(pool)
                .await?;
            if !value.is_null() {
                sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
                    .bind(key)
                    .bind(value.to_string())
                    .execute(pool)
                    .await?;
            }
        }
    }
    if let Some(statuses) = case["tracker_status"].as_object() {
        for (name, status) in statuses {
            let status = status.as_str().ok_or("Expected synthetic tracker status")?;
            sqlx::query("UPDATE event_definitions SET status=$1 WHERE key=$2")
                .bind(status)
                .bind(format!("user.synthetic_chat_{name}"))
                .execute(pool)
                .await?;
        }
    }
    if case["native_reply"].is_object() {
        let proof = &case["native_reply"];
        let key = proof["legacy_key"]
            .as_str()
            .ok_or("Expected synthetic legacy key")?;
        sqlx::query("DELETE FROM app_state WHERE key=$1")
            .bind(key)
            .execute(pool)
            .await?;
        let reference = proof["message_id"]
            .as_i64()
            .ok_or("Expected synthetic provider reference")?
            .to_string();
        let fence =
            (proof["analysis"] == true).then(|| json!({"telegram_diary_analysis":{}}).to_string());
        let changed=sqlx::query(
            "UPDATE outbox_messages SET provider_reference=$1,state='delivered',memory_fence=$2::jsonb
             WHERE id=(SELECT id FROM outbox_messages ORDER BY created_at,id LIMIT 1)"
        ).bind(reference).bind(fence).execute(pool).await?;
        assert_eq!(changed.rows_affected(), 1);
    }
    if case["grant"].is_array() || case["revoke"] == true {
        let definition: Uuid = sqlx::query_scalar(
            "SELECT id FROM event_definitions WHERE key='user.synthetic_chat_sensitive'",
        )
        .fetch_one(pool)
        .await?;
        let scopes: Scopes = [Scope::ManageIntegrations].into_iter().collect();
        let destination = ShareDestination::new(DestinationKind::Channel, "telegram:primary")?;
        timeout(Duration::from_secs(3), async {
            loop {
                let result = if let Some(categories) = case["grant"].as_array() {
                    let categories = categories
                        .iter()
                        .map(|value| match value.as_str() {
                            Some("schema") => Ok(ShareCategory::Schema),
                            Some("facts") => Ok(ShareCategory::Facts),
                            _ => Err("Invalid synthetic grant"),
                        })
                        .collect::<Result<Vec<_>, _>>()
                        .map_err(|_| SharePolicyError::InvalidConsent)?;
                    database
                        .grant_tracker_share(
                            scopes,
                            TrackerShareGrant {
                                definition_id: definition,
                                destination,
                                categories: &categories,
                                granted_at: at,
                            },
                        )
                        .await
                } else {
                    database
                        .revoke_tracker_share(scopes, definition, destination)
                        .await
                        .map(|removed| assert!(removed))
                };
                match result {
                    Err(SharePolicyError::Busy) => sleep(Duration::from_millis(5)).await,
                    other => return other,
                }
            }
        })
        .await??;
        if case["revoke"] == true {
            let queued:i64=sqlx::query_scalar("SELECT count(*) FROM outbox_messages WHERE state='queued' AND intent->'evidence_refs' ? $1").bind(format!("definition:{definition}")).fetch_one(pool).await?;
            assert_eq!(
                queued, 0,
                "Revoked schema-only and fact replies must be cancelled together"
            );
        }
    }
    if case["patch"].is_object() {
        let changed = sqlx::query(
            "UPDATE app_state SET value=value||$1::jsonb WHERE key='conversation:pending'",
        )
        .bind(case["patch"].to_string())
        .execute(pool)
        .await?;
        assert_eq!(changed.rows_affected(), 1);
    }
    if case["chat_form_patch"].is_object() {
        let changed = sqlx::query(
            "UPDATE app_state SET value=jsonb_set(value,'{chat_form}',value->'chat_form'||$1::jsonb)
             WHERE key='conversation:pending'",
        )
        .bind(case["chat_form_patch"].to_string())
        .execute(pool)
        .await?;
        assert_eq!(changed.rows_affected(), 1);
    }
    let callback = if let Some(name) = case["create"].as_str() {
        let version:Uuid=sqlx::query_scalar("SELECT v.id FROM event_definitions d JOIN event_definition_versions v ON v.definition_id=d.id AND v.version=d.current_version WHERE d.key=$1").bind(format!("user.synthetic_chat_{name}")).fetch_one(pool).await?;
        Some(format!("create:{version}"))
    } else if let Some(action) = case["history"].as_str() {
        let name = case["entry"].as_str().ok_or("Expected selected tracker")?;
        let (event, revision): (Uuid, i32) = sqlx::query_as(
            "SELECT id,revision FROM events WHERE kind=$1 ORDER BY start DESC LIMIT 1",
        )
        .bind(format!("user.synthetic_chat_{name}"))
        .fetch_one(pool)
        .await?;
        let token = format!("synthetic_form_{id}");
        sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)").bind(format!("telegram:selection:{token}")).bind(json!({"action":action,"event_id":event.to_string(),"revision":revision,"channel_instance_id":"telegram:primary","expires_at":"2100-01-01T00:00:00+00:00"}).to_string()).execute(pool).await?;
        Some(format!("h:{token}"))
    } else {
        None
    };
    let date = case["source_message_date"]
        .as_i64()
        .unwrap_or_else(|| OffsetDateTime::from(at).unix_timestamp());
    if let Some(callback) = callback {
        Ok(
            json!({"update_id":id,"callback_query":{"id":format!("synthetic-form-{id}"),"data":callback,"from":{"id":42},"message":{"message_id":id,"date":date,"chat":{"id":42,"type":"private"}}}}),
        )
    } else {
        let mut message = case["message"].clone();
        for (key, value) in [
            ("message_id", json!(id)),
            ("date", json!(date)),
            ("from", json!({"id":42})),
            ("chat", json!({"id":42,"type":"private"})),
        ] {
            message[key] = value;
        }
        Ok(json!({"update_id":id,"message":message}))
    }
}

async fn fixture_database() -> Result<(Database, PgPool), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT current_database()")
            .fetch_one(&pool)
            .await?,
        expected
    );
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    Ok((database, pool))
}

#[tokio::test]
#[ignore = "compares native tracker entry forms with the frozen Python conversation on disposable TimescaleDB"]
async fn local_tracker_entry_forms_match_frozen_conversation() -> Result<(), Box<dyn Error>> {
    let locale = env::var("GA_TEST_TRACKER_CHAT_LOCALE").unwrap_or_else(|_| "en".to_owned());
    let (database, pool) = fixture_database().await?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let binding = TelegramBinding {
        owner_id: owner,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let settings = TelegramIngressSettings::selected(Some("42"), None)?
        .ok_or("Expected configured Telegram")?;
    let transport = TelegramTransport::new("100:synthetic-tracker-forms")?;
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TRACKER_CHAT_ENTRIES")?)?)?;
    let probe_target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let at = clock(&pool).await?;
    let probe=wire(&pool,&database,&json!({"message":{"caption":"Explain the unrelated synthetic topic","voice":{"file_id":"synthetic-probe","file_unique_id":"synthetic-probe","duration":1,"file_size":128,"mime_type":"audio/ogg"}}}),70000,at).await?;
    let probe = receive(&mut runtime, binding, &probe, at, true)
        .await?
        .inbound
        .ok_or("Expected caption probe")?;
    let screen = database
        .claim_telegram_local_screen(probe_target, clock(&pool).await?, 10)
        .await?
        .ok_or("Expected caption screening")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            &locale,
        )
        .await?;
    let caption = database
        .claim_telegram_tracker_form(probe_target, clock(&pool).await?, 10, false)
        .await?
        .ok_or("Expected fresh caption claim")?;
    database
        .apply_telegram_tracker_form(&caption, "UTC", &locale, false)
        .await?;
    assert!(
        database
            .claim_telegram_tracker_form(probe_target, clock(&pool).await?, 10, false)
            .await?
            .is_none()
    );
    let probe_state:(String,String,i32,bool)=sqlx::query_as(
        "SELECT i.status,j.status,j.attempts,j.payload->>'tracker_caption_checked'='true'
         FROM inbound_messages i JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id) WHERE i.id=$1"
    ).bind(probe.message_id).fetch_one(&pool).await?;
    assert_eq!(probe_state, ("pending".into(), "pending".into(), 0, true));
    let at = clock(&pool).await?;
    let urgent = wire(
        &pool,
        &database,
        &json!({"message":{"text":"I can't breathe"}}),
        70001,
        at,
    )
    .await?;
    let urgent = receive(&mut runtime, binding, &urgent, at, true)
        .await?
        .inbound
        .ok_or("Expected urgent receipt")?;
    let urgent_reply = async {
        loop {
            let reply:Option<String>=sqlx::query_scalar("SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE inbound_message_id=$1").bind(urgent.message_id).fetch_optional(&pool).await?;
            if let Some(reply) = reply {
                assert!(reply.contains("112"));
                return Ok::<(), Box<dyn Error>>(());
            }
            sleep(Duration::from_millis(10)).await;
        }
    };
    tokio::select! {
        result=timeout(Duration::from_secs(5),urgent_reply)=>result??,
        worker=transport.run_local_text(&database,&settings,&locale,true)=>worker,
    }
    let voice = timeout(Duration::from_secs(3), async {
        loop {
            if let Some(lease) = database
                .claim_telegram_no_model_text(probe_target, clock(&pool).await?, 10)
                .await?
            {
                return Ok::<_, Box<dyn Error>>(lease);
            }
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await??;
    database
        .apply_local_telegram_text(&voice, LocalTelegramText::ProviderUnavailable, &locale)
        .await?;
    println!(
        "Fresh unmatched caption was released once without spending attempts, and a later urgent message completed before voice fallback"
    );
    let provider_available = env::var("GA_TEST_TRACKER_MODEL_AVAILABLE").as_deref() == Ok("true");
    let expected_model_updates: BTreeSet<_> = cases
        .iter()
        .enumerate()
        .filter(|(_, case)| case["reply"] == PROVIDER_UNAVAILABLE_REPLY)
        .map(|(index, _)| (71001 + index).to_string())
        .collect();
    let admitted = AtomicUsize::new(0);
    let model_admission = async {
        if !provider_available {
            return std::future::pending::<Result<(), Box<dyn Error>>>().await;
        }
        let target = TelegramInboundTarget {
            owner_id: owner,
            instance_id: "primary",
        };
        loop {
            if let Some(lease) = database
                .claim_telegram_model_text(target, clock(&pool).await?, 10)
                .await?
            {
                match admission(&database, &lease, &locale).await? {
                    TelegramModelAdmission::Replied => {}
                    TelegramModelAdmission::Ready { .. } => {
                        let envelope = ga_domain::channel::InboundEnvelope::parse(
                            lease.envelope_json().as_bytes(),
                        )?;
                        assert!(
                            expected_model_updates.contains(envelope.external_event_id()),
                            "Unexpected model admission for update {}",
                            envelope.external_event_id()
                        );
                        admitted.fetch_add(1, Ordering::SeqCst);
                        database
                            .finish_telegram_model_notice(&lease, PROVIDER_UNAVAILABLE_REPLY)
                            .await?;
                    }
                }
            }
            sleep(Duration::from_millis(5)).await;
        }
    };
    let proof = async {
        for (index, case) in cases
            .iter()
            .enumerate()
            .filter(|(_, case)| case["recover"] != true)
        {
            let id = 71001 + index as i64;
            let at = clock(&pool).await?;
            let update = wire(&pool, &database, case, id, at).await?;
            let receipt = receive(&mut runtime, binding, &update, at, true)
                .await?
                .inbound
                .ok_or("Missing form receipt")?;
            assert!(receipt.created);
            let text=timeout(Duration::from_secs(5),async {
                loop {
                    let reply:Option<String>=sqlx::query_scalar("SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE inbound_message_id=$1").bind(receipt.message_id).fetch_optional(&pool).await?;
                    if let Some(reply)=reply{return Ok::<String,sqlx::Error>(reply)}
                    sleep(Duration::from_millis(10)).await;
                }
            }).await.map_err(|_|format!("Missing reply at step {index}: {case}"))??;
            assert_eq!(json!(text), case["reply"], "Reply at step {index}: {case}");
            assert_eq!(
                snapshot(&pool, &cases).await?,
                case["snapshot"],
                "State at step {index}: {case}"
            );
            let status:(String,String)=sqlx::query_as("SELECT i.status,j.status FROM inbound_messages i JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id) WHERE i.id=$1").bind(receipt.message_id).fetch_one(&pool).await?;
            assert_eq!(status, ("processed".into(), "done".into()));
            let duplicate = receive(&mut runtime, binding, &update, at, true)
                .await?
                .inbound
                .ok_or("Missing duplicate receipt")?;
            assert!(!duplicate.created);
        }
        println!(
            "Matched local tracker entry worker steps {}",
            cases.len() - 1
        );
        Ok::<(), Box<dyn Error>>(())
    };
    tokio::select! {
        result=proof=>result?,
        worker=transport.run_tracker_forms(&database,&settings,"UTC",&locale,false)=>worker,
        worker=transport.run_tracker_setup(&database,&settings,"UTC",&locale,false)=>worker,
        worker=transport.run_local_text(&database,&settings,&locale,provider_available)=>worker,
        worker=transport.run_local_diary(&database,&settings,"UTC")=>worker,
        result=model_admission=>result?,
    }
    assert_eq!(
        admitted.load(Ordering::SeqCst),
        if provider_available {
            expected_model_updates.len()
        } else {
            0
        }
    );
    let final_case = cases
        .last()
        .filter(|case| case["recover"] == true)
        .ok_or("Expected recovered final submission")?;
    let at = clock(&pool).await?;
    let id = 71001 + cases.len() as i64 - 1;
    let update = wire(&pool, &database, final_case, id, at).await?;
    let receipt = receive(&mut runtime, binding, &update, at, true)
        .await?
        .inbound
        .ok_or("Expected recovery receipt")?;
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let screen = database
        .claim_telegram_local_screen(target, clock(&pool).await?, 10)
        .await?
        .ok_or("Expected screening lease")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await?;
    let abandoned = database
        .claim_telegram_model_text(target, clock(&pool).await?, 10)
        .await?
        .ok_or("Expected abandoned form lease")?;
    sqlx::query("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1 AND lease_token=$2").bind(abandoned.job_identity().0).bind(abandoned.job_identity().1).execute(&pool).await?;
    let recovered = database
        .claim_telegram_model_text(target, clock(&pool).await?, 10)
        .await?
        .ok_or("Expected recovered form lease")?;
    let before = snapshot(&pool, &cases).await?;
    assert!(matches!(
        admission(&database, &abandoned, &locale).await,
        Err(TelegramDiaryCommitError::Job(
            ga_db::jobs::JobStoreError::LeaseLost
        ))
    ));
    assert_eq!(snapshot(&pool, &cases).await?, before);
    assert!(matches!(
        admission(&database, &recovered, &locale).await?,
        TelegramModelAdmission::Replied
    ));
    assert_eq!(snapshot(&pool, &cases).await?, final_case["snapshot"]);
    let replies: Vec<String> = sqlx::query_scalar(
        "SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE inbound_message_id=$1",
    )
    .bind(receipt.message_id)
    .fetch_all(&pool)
    .await?;
    assert_eq!(json!(replies), json!([final_case["reply"]]));
    assert!(matches!(
        admission(&database, &recovered, &locale).await,
        Err(TelegramDiaryCommitError::Job(
            ga_db::jobs::JobStoreError::LeaseLost
        ))
    ));
    assert_eq!(snapshot(&pool, &cases).await?, final_case["snapshot"]);
    let duplicate = receive(&mut runtime, binding, &update, at, true)
        .await?
        .inbound
        .ok_or("Expected duplicate recovery receipt")?;
    assert!(!duplicate.created);
    println!(
        "Matched local tracker entry steps {} and recovered the final tracker step exactly once",
        cases.len()
    );
    if let Ok(status) = fs::read_to_string("/proc/self/status") {
        for line in status
            .lines()
            .filter(|line| line.starts_with("VmHWM:") || line.starts_with("VmRSS:"))
        {
            println!("{line}");
        }
    }
    for name in ["memory.peak", "memory.swap.peak", "memory.events"] {
        if let Ok(value) = fs::read_to_string(format!("/sys/fs/cgroup/{name}")) {
            println!("{name}\n{value}");
        }
    }
    Ok(())
}

#[tokio::test]
#[ignore = "compares language changes and old pending tracker state on disposable TimescaleDB"]
async fn native_tracker_forms_match_frozen_locale_transitions() -> Result<(), Box<dyn Error>> {
    let (database, pool) = fixture_database().await?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let binding = TelegramBinding {
        owner_id: owner,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TRACKER_CHAT_ENTRIES")?)?)?;
    for (index, case) in cases.iter().enumerate() {
        let locale = case["locale"].as_str().ok_or("Expected locale")?;
        let now = clock(&pool).await?;
        let update = wire(&pool, &database, case, 71001 + index as i64, now).await?;
        let receipt = receive(&mut runtime, binding, &update, now, true)
            .await?
            .inbound
            .ok_or("Expected tracker update")?;
        if case["message"].is_object() {
            let screen = database
                .claim_telegram_local_screen(target, clock(&pool).await?, 10)
                .await?
                .ok_or("Expected local screening")?;
            database
                .apply_local_telegram_text(
                    &screen,
                    LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
                    locale,
                )
                .await?;
            let lease = database
                .claim_telegram_model_text(target, clock(&pool).await?, 10)
                .await?
                .ok_or_else(|| format!("Expected screened tracker work at step {index}"))?;
            assert!(matches!(
                admission(&database, &lease, locale).await?,
                TelegramModelAdmission::Replied
            ));
        } else {
            let lease = database
                .claim_telegram_tracker_form(target, clock(&pool).await?, 10, false)
                .await?
                .ok_or_else(|| format!("Expected tracker callback at step {index}"))?;
            database
                .apply_telegram_tracker_form(&lease, "UTC", locale, false)
                .await?;
        }
        let reply: String = sqlx::query_scalar(
            "SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE inbound_message_id=$1",
        )
        .bind(receipt.message_id)
        .fetch_one(&pool)
        .await?;
        assert_eq!(json!(reply), case["reply"], "Reply at step {index}");
        assert_eq!(
            snapshot(&pool, &cases).await?,
            case["snapshot"],
            "Stored tracker state at step {index}"
        );
    }
    println!("Matched {} locale-change and old-state steps", cases.len());
    Ok(())
}

async fn admission(
    database: &Database,
    lease: &TelegramInboundLease,
    locale: &str,
) -> Result<TelegramModelAdmission, TelegramDiaryCommitError> {
    timeout(Duration::from_secs(3), async {
        loop {
            match database
                .admit_telegram_model_text(lease, "UTC", locale)
                .await
            {
                Err(TelegramDiaryCommitError::Outbox(
                    ga_db::outbox_enqueue::QueueIntentError::Busy,
                )) => sleep(Duration::from_millis(5)).await,
                result => return result,
            }
        }
    })
    .await
    .unwrap_or(Err(TelegramDiaryCommitError::Database))
}
