use std::{env, error::Error, fs};

use ga_db::{
    Database,
    proactive_control::{
        NotificationDecision, NotificationExclusions, NotificationPolicy, NotificationReason,
        ProactiveControlError,
    },
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use sqlx::postgres::PgConnectOptions;
use time::{Time, format_description::well_known::Iso8601};

const ZONE: &str = "Europe/Bratislava";

#[derive(Deserialize)]
struct Case {
    name: String,
    now: String,
    destination: Option<String>,
    owner: Option<bool>,
    default_enabled: bool,
    inbound: bool,
    snooze: Option<String>,
    budget: Option<u8>,
    quiet_start: String,
    quiet_end: String,
    action: String,
    reason: String,
    retry_at: Option<String>,
}

#[tokio::test]
#[ignore = "requires migration/check_notification_policy.py and its disposable database"]
async fn respects_live_notification_policy() -> Result<(), Box<dyn Error>> {
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
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_NOTIFICATION_POLICY")?)?)?;
    assert!(cases.len() >= 17);
    for case in &cases {
        if let Some(enabled) = case.owner {
            sqlx::query(
                "INSERT INTO app_state(key,value) VALUES('proactive:enabled', jsonb_build_object('enabled',$1::boolean))
                 ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            )
            .bind(enabled)
            .execute(&pool)
            .await?;
        } else {
            sqlx::query("DELETE FROM app_state WHERE key='proactive:enabled'")
                .execute(&pool)
                .await?;
        }
        sqlx::query("UPDATE telegram_updates SET status=$1 WHERE id=771")
            .bind(if case.inbound { "pending" } else { "processed" })
            .execute(&pool)
            .await?;
        let now: StoredInstant = case.now.parse()?;
        let settings = NotificationPolicy {
            timezone: ZONE,
            default_enabled: case.default_enabled,
            daily_budget: case.budget,
            quiet_start: Time::parse(&case.quiet_start, &Iso8601::TIME)?,
            quiet_end: Time::parse(&case.quiet_end, &Iso8601::TIME)?,
            snoozed_until: case.snooze.as_deref().map(str::parse).transpose()?,
            destination: case.destination.as_deref(),
            exclusions: NotificationExclusions::default(),
        };
        let result = database.notification_decision(&settings, now).await?;
        let actual = match result {
            NotificationDecision::Allow => ("allow", "allowed", None),
            NotificationDecision::Cancel(reason) => ("cancel", reason.label(), None),
            NotificationDecision::Defer { reason, retry_at } => {
                assert!(retry_at > now, "{}", case.name);
                ("defer", reason.label(), Some(retry_at))
            }
        };
        assert_eq!(
            actual,
            (
                case.action.as_str(),
                case.reason.as_str(),
                case.retry_at.as_deref().map(str::parse).transpose()?,
            ),
            "{}",
            case.name
        );
    }

    let daytime = NotificationPolicy {
        timezone: ZONE,
        default_enabled: true,
        daily_budget: None,
        quiet_start: Time::MIDNIGHT,
        quiet_end: Time::MIDNIGHT,
        snoozed_until: None,
        destination: Some("telegram:unrelated"),
        exclusions: NotificationExclusions::default(),
    };
    let now: StoredInstant = cases[0].now.parse()?;
    sqlx::query(
        "WITH input AS (
             INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,
                external_event_id,sender_ref,received_at,kind,envelope,revision,status,operation_id)
             SELECT gen_random_uuid(),owner_id,id,'telegram','fixture','123','42',
                current_timestamp,'text','{}',1,'pending',gen_random_uuid()
             FROM conversations WHERE channel='telegram' AND channel_instance_id='fixture'
             RETURNING id
         )
         INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         SELECT gen_random_uuid(),'telegram_native_inbound',
            jsonb_build_object('inbound_message_id',id),concat('telegram:inbound:native:',id),
            'running',current_timestamp,1 FROM input",
    )
    .execute(&pool)
    .await?;
    assert!(matches!(
        database.notification_decision(&daytime, now).await?,
        NotificationDecision::Defer {
            reason: NotificationReason::InboundPending,
            ..
        }
    ));
    sqlx::query("UPDATE jobs SET status='completed' WHERE kind='telegram_native_inbound'")
        .execute(&pool)
        .await?;
    let mut competing = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *competing)
        .await?;
    assert!(matches!(
        database.notification_decision(&daytime, now).await,
        Err(ProactiveControlError::Busy)
    ));
    competing.rollback().await?;
    assert_eq!(
        database.notification_decision(&daytime, now).await?,
        NotificationDecision::Allow
    );
    println!(
        "Verified {} notification decisions, native inbound deferral and the mutation lock",
        cases.len()
    );
    Ok(())
}
