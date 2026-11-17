use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    personal_goals::{GoalCommandOrder, GoalSelection, PersonalGoal, PersonalGoalsError},
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    revision: u64,
    goals: Vec<PersonalGoal>,
    at: String,
    command: Option<Command>,
    value: Value,
    result: Value,
}

#[derive(Deserialize)]
struct Command {
    source_epoch: i64,
    received_at: String,
    tie_breaker: String,
}

#[tokio::test]
#[ignore = "uses a pinned Python reference and a disposable TimescaleDB"]
async fn preserves_explicit_goal_preferences_and_command_order() -> Result<(), Box<dyn Error>> {
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
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(expected, actual);
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    assert_eq!(database.personal_goals(owner).await?, Default::default());
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_PERSONAL_GOALS")?)?)?;

    for (index, case) in cases.iter().enumerate() {
        let command = case
            .command
            .as_ref()
            .map(|order| {
                Ok::<_, Box<dyn Error>>(GoalCommandOrder {
                    source_epoch: order.source_epoch,
                    received_at: order.received_at.parse()?,
                    tie_breaker: &order.tie_breaker,
                })
            })
            .transpose()?;
        let selected = database
            .select_personal_goals(
                owner,
                GoalSelection {
                    expected_revision: case.revision,
                    goals: &case.goals,
                    now: case.at.parse()?,
                    command,
                },
            )
            .await;
        let actual = match selected {
            Ok(result) => serde_json::to_value(result)?,
            Err(PersonalGoalsError::Conflict) => Value::String("conflict".into()),
            Err(PersonalGoalsError::InvalidSelection) => Value::String("invalid".into()),
            Err(error) => return Err(error.into()),
        };
        assert_eq!(actual, case.result, "result {index}");
        let stored: String = sqlx::query_scalar(
            "SELECT (value-'owner_id')::text FROM app_state WHERE key='preferences:personal-goals'",
        )
        .fetch_one(&pool)
        .await?;
        let actual: Value = serde_json::from_str(&stored)?;
        assert_eq!(actual, case.value, "stored state {index}");
    }

    let mut sending = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock_shared(72104626)")
        .execute(&mut *sending)
        .await?;
    let current = database.personal_goals(owner).await?;
    let now: StoredInstant = "2026-11-16T12:00:01Z".parse()?;
    assert!(matches!(
        database
            .select_personal_goals(
                owner,
                GoalSelection {
                    expected_revision: current.revision,
                    goals: &[PersonalGoal::Running],
                    now,
                    command: None,
                }
            )
            .await,
        Err(PersonalGoalsError::Busy)
    ));
    assert_eq!(database.personal_goals(owner).await?, current);
    sending.rollback().await?;
    assert!(matches!(
        database.personal_goals(Uuid::from_u128(1)).await,
        Err(PersonalGoalsError::Owner)
    ));
    println!(
        "{} goal preference snapshots matched pinned Python and the delivery guard held",
        cases.len()
    );
    Ok(())
}
