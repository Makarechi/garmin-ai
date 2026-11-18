use std::{env, error::Error, fs};

use ga_db::{
    Database,
    proactive_control::{NotificationExclusions, ProactiveControlError},
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use sqlx::postgres::PgConnectOptions;
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    name: String,
    now: String,
    timezone: String,
    outbox: Option<String>,
    insight_key: Option<String>,
    count: i64,
}

#[tokio::test]
#[ignore = "requires migration/check_notification_budget.py and its disposable PostgreSQL database"]
async fn matches_the_existing_cross_channel_budget() -> Result<(), Box<dyn Error>> {
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
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_NOTIFICATION_BUDGET")?)?)?;
    assert!(cases.len() > 8);
    for case in &cases {
        let now: StoredInstant = case.now.parse()?;
        let count = database
            .notification_usage(
                now,
                &case.timezone,
                NotificationExclusions {
                    outbox_id: case.outbox.as_deref().map(str::parse::<Uuid>).transpose()?,
                    insight_key: case.insight_key.as_deref(),
                },
            )
            .await?;
        assert_eq!(count, case.count, "{}", case.name);
    }

    let lock = sqlx::PgPool::connect_with(parsed).await?;
    let mut txn = lock.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104621)")
        .execute(&mut *txn)
        .await?;
    let case = &cases[0];
    let now = case.now.parse()?;
    assert!(matches!(
        database
            .notification_usage(now, &case.timezone, NotificationExclusions::default())
            .await,
        Err(ProactiveControlError::Busy)
    ));
    txn.rollback().await?;
    assert_eq!(
        database
            .notification_usage(now, &case.timezone, NotificationExclusions::default())
            .await?,
        case.count
    );
    println!(
        "Verified {} cross-channel budget cases and the shared lock",
        cases.len()
    );
    Ok(())
}
