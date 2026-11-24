use std::{env, error::Error, future::ready};

use ga_db::{
    Database,
    health_analysis::HealthPeriod,
    outbox_claims::ReplyDestination,
    proactive_control::{
        DiaryQuestionOutcome, NotificationExclusions, NotificationPolicy, ProactiveControlError,
    },
    replay::GARMIN_PARSER_VERSION,
};
use ga_domain::timestamp::StoredInstant;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Date, Month, OffsetDateTime, Time};
use uuid::Uuid;

#[tokio::test]
#[ignore = "requires a freshly migrated disposable TimescaleDB"]
async fn empty_polls_skip_archive_reads_and_candidates_keep_the_replay_gate()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture database URL")?;
    let options = format!("postgresql://{address}")
        .parse::<PgConnectOptions>()?
        .disable_statement_logging();
    if !options
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
        || !matches!(options.get_host(), "localhost" | "127.0.0.1" | "::1")
    {
        return Err("Use a disposable loopback database ending in _test".into());
    }
    let pool = PgPool::connect_with(options).await?;
    let empty: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS(SELECT 1 FROM insights)
            AND NOT EXISTS(SELECT 1 FROM source_payloads)
            AND NOT EXISTS(SELECT 1 FROM health_days)",
    )
    .fetch_one(&pool)
    .await?;
    assert!(empty, "Use a freshly migrated fixture database");
    sqlx::raw_sql(
        "INSERT INTO people(id,locale,timezone,units)
         VALUES(gen_random_uuid(),'en','UTC','metric') ON CONFLICT(singleton) DO NOTHING;
         INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         SELECT gen_random_uuid(),id,'telegram','primary','42','fixture' FROM people;
         INSERT INTO app_state(key,value) VALUES
           ('preferences:onboarding','{\"channel\":{\"channel\":\"telegram\",\"instance_id\":\"primary\"}}'),
           ('proactive:enabled','{\"enabled\":true}');
         UPDATE module_configs SET tracking_enabled=true,reminders_enabled=true WHERE pack_key='sleep';
         INSERT INTO health_days(day,sleep_score,sources)
         SELECT DATE '2026-11-01'+i,CASE WHEN i<14 THEN 50 ELSE 80 END+i%4,'{}'
         FROM generate_series(0,27) AS days(i);",
    )
    .execute(&pool)
    .await?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let now: StoredInstant = "2026-11-29T12:00:00Z".parse()?;
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "telegram",
        instance_id: "primary",
    };
    let policy = NotificationPolicy {
        timezone: "UTC",
        default_enabled: true,
        daily_budget: None,
        quiet_start: Time::MIDNIGHT,
        quiet_end: Time::MIDNIGHT,
        snoozed_until: None,
        destination: None,
        exclusions: NotificationExclusions::default(),
    };

    // This lock blocks any archive selection without relying on timing thresholds.
    let mut blocked_archive = pool.begin().await?;
    sqlx::query("LOCK TABLE source_payloads IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *blocked_archive)
        .await?;
    assert!(
        !database
            .dispatch_trend_notification(destination(), 42, &policy, now, |_| never_send())
            .await?
    );

    let insight = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO insights(id,category,statement,evidence,sample_size,status,dedup_key,generated_at)
         VALUES($1,'trend','Synthetic sleep trend','{}',28,'accepted','trend:sleep_score:fixture',$2)",
    )
    .bind(insight)
    .bind(OffsetDateTime::from(now))
    .execute(&pool)
    .await?;
    assert!(matches!(
        database
            .dispatch_trend_notification(destination(), 42, &policy, now, |_| never_send())
            .await,
        Err(ProactiveControlError::Database)
    ));
    blocked_archive.rollback().await?;

    let source = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,payload,archive_key,parser_version,status)
         VALUES($1,'garmin_connect','sleep','2026-11-28','synthetic','{}','fixture/sleep.json',$2,'captured')",
    )
    .bind(source)
    .bind(GARMIN_PARSER_VERSION - 1)
    .execute(&pool)
    .await?;
    assert!(!database.replay_status(GARMIN_PARSER_VERSION).await?.ready);
    assert!(
        !database
            .dispatch_trend_notification(destination(), 42, &policy, now, |_| never_send())
            .await?
    );
    let status: String = sqlx::query_scalar("SELECT status FROM insights WHERE id=$1")
        .bind(insight)
        .fetch_one(&pool)
        .await?;
    assert_eq!(status, "accepted");
    let reserved: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state
          WHERE key=$1 OR key='insight:last:sleep_score')",
    )
    .bind(format!("outbox:insight:{insight}:0"))
    .fetch_one(&pool)
    .await?;
    assert!(!reserved);

    sqlx::query("UPDATE source_payloads SET parser_version=$2 WHERE id=$1")
        .bind(source)
        .bind(GARMIN_PARSER_VERSION)
        .execute(&pool)
        .await?;
    let period = |start, end| {
        HealthPeriod::new(
            Date::from_calendar_date(2026, Month::November, start)?,
            Date::from_calendar_date(2026, Month::November, end)?,
        )
        .map_err(Box::<dyn Error>::from)
    };
    let evidence = database
        .compare_health_periods("sleep_score", period(15, 28)?, period(1, 14)?)
        .await?;
    sqlx::query("UPDATE insights SET evidence=$2::jsonb WHERE id=$1")
        .bind(insight)
        .bind(evidence.to_string())
        .execute(&pool)
        .await?;
    assert!(
        database
            .dispatch_trend_notification(destination(), 42, &policy, now, |text| {
                assert_eq!(text, "Synthetic sleep trend");
                ready(DiaryQuestionOutcome::Accepted(701))
            })
            .await?
    );
    let status: String = sqlx::query_scalar("SELECT status FROM insights WHERE id=$1")
        .bind(insight)
        .fetch_one(&pool)
        .await?;
    assert_eq!(status, "delivered");
    assert!(
        !database
            .dispatch_trend_notification(destination(), 42, &policy, now, |_| never_send())
            .await?
    );
    Ok(())
}

fn never_send() -> std::future::Ready<DiaryQuestionOutcome> {
    panic!("No notification should be sent in this state")
}
