use std::{env, error::Error, time::Duration};

use ga_db::{Database, diary::CreateSystemEvent};
use ga_domain::{
    event_input::{EventInput, ValidationMode},
    timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{OffsetDateTime, Time};
use tokio::{
    sync::watch,
    time::{sleep, timeout},
};
use uuid::Uuid;

use super::{bind, request, respond};
use crate::{
    telegram_settings::TelegramIngressSettings, telegram_transport::DiaryReminderSettings,
};

#[tokio::test]
#[ignore = "runs native reminder and receiver lanes against a disposable PostgreSQL and loopback Telegram"]
async fn worker_recovers_and_sends_one_current_reminder() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let raw = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options = format!("postgresql://{raw}")
        .parse::<PgConnectOptions>()?
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
    sqlx::raw_sql(
        "INSERT INTO people(id,locale,timezone,units)
         VALUES(gen_random_uuid(),'ru','UTC','metric') ON CONFLICT(singleton) DO NOTHING;
         INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         SELECT gen_random_uuid(),id,'telegram','primary','42','fixture' FROM people;
         INSERT INTO app_state(key,value) VALUES
           ('preferences:onboarding','{\"channel\":{\"channel\":\"telegram\",\"instance_id\":\"primary\"},\"model_categories\":[\"diary\"]}'),
           ('proactive:enabled','{\"enabled\":true}');",
    ).execute(&pool).await?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    sqlx::query(
        "UPDATE app_state SET value=value-'model_categories' WHERE key='preferences:onboarding'",
    )
    .execute(&pool)
    .await?;
    assert!(
        !database
            .telegram_diary_reminders_enabled(owner, "primary", true)
            .await?
    );
    sqlx::query("UPDATE app_state SET value=value || '{\"model_categories\":[\"diary\"]}'::jsonb WHERE key='preferences:onboarding'")
        .execute(&pool).await?;
    assert!(
        database
            .telegram_diary_reminders_enabled(owner, "primary", false)
            .await?
    );
    let clock = OffsetDateTime::now_utc();
    let now = clock.replace_nanosecond(clock.microsecond() * 1_000)?;
    let start = StoredInstant::try_from(now - time::Duration::hours(3))?;
    let raw = json!({"start":start.to_string(),"timezone":"UTC","payload":{"type":"migraine","severity":5}});
    let report =
        EventInput::from_json(&serde_json::to_vec(&raw)?, 1024, ValidationMode::NewReport)?;
    let event = database
        .create_system_event(CreateSystemEvent {
            owner_id: owner,
            authenticated_actor: "fixture",
            report: &report,
            idempotency_key: None,
            operation_id: None,
            clock_uncertainty_seconds: None,
        })
        .await?;
    let unknown = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO pending_questions(id,kind,text,evidence,priority,dedup_key,earliest_send_at,
              expires_at,sent_at,attempts,status)
         VALUES($1,'caffeine','Synthetic prior question','{}',0.6,'fixture:ambiguous',
                $2::timestamptz,$2+interval '48 hours',$2,1,'sending')",
    )
    .bind(unknown)
    .bind(now)
    .execute(&pool)
    .await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES($1,'{\"status\":\"sending\"}')")
        .bind(format!("outbox:question:{unknown}:0"))
        .execute(&pool)
        .await?;

    let (listener, transport) = bind().await?;
    let binding =
        TelegramIngressSettings::selected(Some("42"), None)?.ok_or("Missing fixture bot")?;
    let settings = DiaryReminderSettings {
        timezone: "UTC".into(),
        enabled: false,
        daily_budget: 2,
        quiet_start: Time::MIDNIGHT,
        quiet_end: Time::MIDNIGHT,
    };
    let mut runtime = database.acquire_runtime_lease().await?;
    let (recovered, ready) = watch::channel(false);
    let worker = async {
        tokio::select! {
            state = transport.run_receiver_with_reminders(&database, &mut runtime, &binding, recovered) => {
                state?;
                Err::<(), Box<dyn Error>>("Receiver exited before fixture verification".into())
            }
            never = transport.run_diary_reminders(&database, &binding, &settings, ready) => never,
        }
    };
    let observer = async {
        for pending in [0, 1, 0, 0] {
            let (mut webhook, _) = listener.accept().await?;
            assert_eq!(request(&mut webhook).await?["endpoint"], "getWebhookInfo");
            let response = json!({"ok":true,"result":{
                "url":"https://synthetic.invalid/telegram", "pending_update_count":pending,
            }});
            respond(&mut webhook, "200 OK", &[], &response.to_string()).await?;
        }
        let (mut stream, _) = listener.accept().await?;
        let body = request(&mut stream).await?;
        assert_eq!(body["endpoint"], "sendMessage");
        assert_eq!(body["fields"]["chat_id"], 42);
        let question = body["fields"]["text"]
            .as_str()
            .ok_or("Expected plain question text")?;
        assert!(question.starts_with("Мигрень, начавшаяся"));
        assert!(question.ends_with("Принимали ли что-нибудь?"));
        let recovered: String =
            sqlx::query_scalar("SELECT status FROM pending_questions WHERE id=$1")
                .bind(unknown)
                .fetch_one(&pool)
                .await?;
        assert_eq!(recovered, "uncertain");
        respond(
            &mut stream,
            "200 OK",
            &[],
            r#"{"ok":true,"result":{"message_id":761,"chat":{"id":42,"type":"private"}}}"#,
        )
        .await?;
        loop {
            let completed: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM pending_questions q JOIN app_state a
                     ON a.key='outbox:question:' || q.id::text || ':0'
                 WHERE q.event_id=$1 AND q.status='sent' AND a.value->>'message_id'='761')",
            )
            .bind(event.id)
            .fetch_one(&pool)
            .await?;
            if completed {
                break;
            }
            sleep(Duration::from_millis(10)).await;
        }
        assert!(
            timeout(Duration::from_millis(200), listener.accept())
                .await
                .is_err()
        );
        Ok::<(), Box<dyn Error>>(())
    };
    tokio::select! {
        result = worker => result?,
        result = timeout(Duration::from_secs(8), observer) => result??,
    }
    println!(
        "Verified startup recovery, webhook backlog, saved enablement, and a single Telegram send"
    );
    Ok(())
}
