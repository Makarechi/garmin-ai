use std::{env, error::Error, io, str::FromStr};

use ga_db::{
    Database,
    scenario_packs::{PackSelection, ScenarioPackError},
};
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses disposable TimescaleDB with synthetic pending questions and context"]
async fn matches_the_pinned_pack_defaults_and_revisions() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected PostgreSQL fixture URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    if !options
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
    {
        return Err("Use a disposable database ending in _test".into());
    }
    let inspect = PgPool::connect_with(options).await?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&inspect)
        .await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let expected: Value = serde_json::from_str(&env::var("GA_TEST_SCENARIO_EXPECTED")?)?;
    let initial_epoch: Uuid = sqlx::query_scalar("SELECT memory_epoch FROM conversations")
        .fetch_one(&inspect)
        .await?;
    assert!(matches!(
        db.scenario_packs(owner, false).await,
        Err(ScenarioPackError::Forbidden)
    ));
    assert!(matches!(
        db.scenario_packs(Uuid::new_v4(), true).await,
        Err(ScenarioPackError::UnknownOwner)
    ));
    let observed = db.scenario_packs(owner, true).await?;
    assert_eq!(Value::Array(observed), expected["initial"]);

    let enabled = PackSelection {
        revision: 1,
        tracking_enabled: true,
        collection_enabled: false,
        reminders_enabled: false,
        visible: true,
        llm_enabled: true,
        outcome_goal: Some("Синтетическая цель".to_owned()),
    };
    assert!(matches!(
        db.configure_scenario_pack(owner, false, "caffeine", &enabled)
            .await,
        Err(ScenarioPackError::Forbidden)
    ));
    assert_eq!(
        db.configure_scenario_pack(owner, true, "caffeine", &enabled)
            .await?,
        expected["enabled"]
    );
    assert!(matches!(
        db.configure_scenario_pack(owner, true, "caffeine", &enabled)
            .await,
        Err(ScenarioPackError::RevisionChanged)
    ));
    let disabled = PackSelection {
        revision: 2,
        tracking_enabled: false,
        llm_enabled: false,
        reminders_enabled: true,
        ..enabled
    };

    let mut delivery_fence = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock_shared(72104643)")
        .execute(&mut *delivery_fence)
        .await?;
    assert!(
        db.configure_scenario_pack(owner, true, "caffeine", &disabled)
            .await
            .is_err()
    );
    delivery_fence.rollback().await?;
    assert_eq!(
        Value::Array(db.scenario_packs(owner, true).await?),
        expected["while_enabled"]
    );

    assert_eq!(
        db.configure_scenario_pack(owner, true, "caffeine", &disabled)
            .await?,
        expected["disabled"]
    );
    let final_epoch: Uuid = sqlx::query_scalar("SELECT memory_epoch FROM conversations")
        .fetch_one(&inspect)
        .await?;
    assert_ne!(initial_epoch, final_epoch);
    Ok(())
}
