use std::{env, error::Error, fs};

use ga_db::{
    Database,
    diary::custom_entries::CreateCustomEntry,
    outbox_claims::{ReplyDestination, ReplyDispatchError, ReplyDispatchResult},
    proactive_control::{
        NotificationExclusions, NotificationPolicy, ProactiveCommand, ProactiveControlError,
        TrackerReminderRoute,
    },
};
use ga_domain::{event_schema::CustomEntry, timestamp::StoredInstant};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::postgres::PgConnectOptions;
use time::{OffsetDateTime, Time};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    owner: String,
    now: String,
    chat: i64,
    rules: Value,
    outbox: Value,
}

#[tokio::test]
#[ignore = "requires migration/check_tracker_checkins.py and its disposable database"]
async fn matches_python_tracker_checkins() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let suffix = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected test database URL")?;
    let parsed: PgConnectOptions = format!("postgresql://{suffix}").parse()?;
    if !parsed
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
    {
        return Err("Expected a disposable test database".into());
    }
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let pool = sqlx::PgPool::connect_with(parsed).await?;
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TRACKER_CHECKINS")?)?)?;
    let owner = fixture.owner.parse::<Uuid>()?;
    let now = fixture.now.parse::<StoredInstant>()?;
    let route = || TrackerReminderRoute {
        owner,
        telegram_instance: "primary",
        telegram_chat: fixture.chat,
    };
    let policy = NotificationPolicy {
        timezone: "Europe/Bratislava",
        default_enabled: false,
        daily_budget: Some(3),
        quiet_start: Time::from_hms(22, 0, 0)?,
        quiet_end: Time::from_hms(6, 0, 0)?,
        snoozed_until: None,
        destination: None,
        exclusions: NotificationExclusions::default(),
    };
    assert!(matches!(
        database
            .queue_tracker_checkins(
                TrackerReminderRoute {
                    telegram_chat: 918_725,
                    ..route()
                },
                &policy,
                now,
                None
            )
            .await,
        Err(ProactiveControlError::UnknownOwner)
    ));
    let page = database
        .queue_tracker_checkins(route(), &policy, now, None)
        .await?;
    assert_eq!(page.queued, 1);
    assert_eq!(page.next, None);
    let repeated = database
        .queue_tracker_checkins(route(), &policy, now, None)
        .await?;
    assert_eq!(repeated.queued, 0);
    let rules: Vec<(String, String)> =
        sqlx::query_as("SELECT key,value::text FROM app_state WHERE key LIKE 'initiative:rule:%'")
            .fetch_all(&pool)
            .await?;
    let saved = rules
        .into_iter()
        .map(|(key, value)| Ok((key, serde_json::from_str(&value)?)))
        .collect::<Result<Map<String, Value>, serde_json::Error>>()?;
    assert_eq!(Value::Object(saved), fixture.rules);
    let outbox: Vec<(String, String, String, Option<OffsetDateTime>)> = sqlx::query_as(
        "SELECT dedup_key,state,(intent - 'intent_id')::text,next_attempt_at
             FROM outbox_messages ORDER BY dedup_key",
    )
    .fetch_all(&pool)
    .await?;
    let actual = outbox
        .into_iter()
        .map(|(key, state, raw, retry)| {
            let retry = retry
                .map(StoredInstant::try_from)
                .transpose()?
                .map(|time| time.to_string());
            let intent: Value = serde_json::from_str(&raw)?;
            Ok(json!({"key":key,"state":state,"intent":intent,"retry":retry}))
        })
        .collect::<Result<Vec<_>, Box<dyn Error>>>()?;
    assert_eq!(Value::Array(actual), fixture.outbox);
    println!(
        "Matched the saved rule, reminder text, strict consent, quiet-hour recovery, and retry deduplication"
    );

    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let start = now - time::Duration::minutes(3);
    let now = StoredInstant::try_from(now)?;
    let current_policy = NotificationPolicy {
        timezone: "UTC",
        quiet_start: Time::MIDNIGHT,
        quiet_end: Time::MIDNIGHT,
        ..policy
    };
    let clock = format!("{:02}:{:02}", start.hour(), start.minute());
    sqlx::query(
        "UPDATE tracker_configs SET reminder_time=$1,reminder_timezone='UTC',revision=revision+1",
    )
    .bind(clock)
    .execute(&pool)
    .await?;
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "telegram",
        instance_id: "primary",
    };

    assert_eq!(
        database
            .queue_tracker_checkins(route(), &current_policy, now, None)
            .await?
            .queued,
        1
    );
    assert!(
        database
            .claim_outbox_reply(destination(), now, 90)
            .await?
            .is_none()
    );
    let claim = database
        .claim_telegram_tracker_checkin(destination(), now, 90)
        .await?
        .ok_or("Expected a due fixture check-in")?;
    let never_send =
        |_| async { panic!("An invalidated check-in must not be exposed to the provider") };
    database
        .set_proactive_control(owner, ProactiveCommand::Web { received_at: now }, false)
        .await?;
    assert_eq!(
        database
            .dispatch_telegram_tracker_checkin(destination(), fixture.chat, claim, never_send)
            .await?,
        ReplyDispatchResult::Cancelled
    );

    let resumed = StoredInstant::from_unix_microseconds(now.unix_microseconds() + 1)?;
    database
        .set_proactive_control(
            owner,
            ProactiveCommand::Web {
                received_at: resumed,
            },
            true,
        )
        .await?;
    sqlx::query("UPDATE tracker_configs SET revision=revision+1")
        .execute(&pool)
        .await?;
    assert_eq!(
        database
            .queue_tracker_checkins(route(), &current_policy, resumed, None)
            .await?
            .queued,
        1
    );
    let claim = database
        .claim_telegram_tracker_checkin(destination(), resumed, 90)
        .await?
        .ok_or("Expected a due fixture check-in")?;
    let mut replay = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *replay)
        .await?;
    assert!(matches!(
        database
            .dispatch_telegram_tracker_checkin(destination(), fixture.chat, claim, never_send)
            .await,
        Err(ReplyDispatchError::Busy)
    ));
    replay.rollback().await?;

    let entry = CustomEntry::from_value(json!({
        "definition_key":"user.private_fixture","start":StoredInstant::try_from(start)?.to_string(),
        "timezone":"UTC","values":{"value":3},"source":"manual",
    }))?;
    database
        .create_custom_entry(CreateCustomEntry {
            owner_id: owner,
            actor: "synthetic",
            entry: &entry,
            idempotency_key: None,
            evidence_refs: &[],
        })
        .await?;
    assert_eq!(
        database
            .dispatch_telegram_tracker_checkin(destination(), fixture.chat, claim, never_send)
            .await?,
        ReplyDispatchResult::Cancelled
    );
    assert_eq!(
        database
            .queue_tracker_checkins(route(), &current_policy, resumed, None)
            .await?
            .queued,
        0
    );
    println!(
        "Verified current pause, replay lock, and a newly recorded tracker entry at send time"
    );
    Ok(())
}
