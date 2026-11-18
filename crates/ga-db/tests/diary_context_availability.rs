use std::{collections::BTreeMap, env, error::Error, fs, str::FromStr};

use ga_db::{Database, proactive_control::DiaryContextAvailability};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;

#[derive(Deserialize)]
struct Fixture {
    now: String,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    jobs: Vec<Job>,
    state: BTreeMap<String, Value>,
    expected: String,
}

#[derive(Deserialize)]
struct Job {
    kind: String,
    payload: Value,
    status: Option<String>,
    completed_at: Option<String>,
}

#[tokio::test]
#[ignore = "compares the pinned Python job gate on a disposable TimescaleDB"]
async fn matches_garmin_context_dependencies() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected disposable database URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_CONTEXT_AVAILABILITY")?)?)?;
    let now: StoredInstant = fixture.now.parse()?;
    for case in fixture.cases {
        let mut setup = pool.begin().await?;
        for (index, job) in case.jobs.iter().enumerate() {
            let completed: Option<OffsetDateTime> = job
                .completed_at
                .as_deref()
                .map(str::parse::<StoredInstant>)
                .transpose()?
                .map(Into::into);
            sqlx::query(
                "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,completed_at,attempts)
                 VALUES(gen_random_uuid(),$1,$2::jsonb,$3,$4,$5,$6,0)",
            )
            .bind(&job.kind)
            .bind(job.payload.to_string())
            .bind(format!("context-gate:{}:{index}", case.name))
            .bind(job.status.as_deref().unwrap_or("pending"))
            .bind(OffsetDateTime::from(now))
            .bind(completed)
            .execute(&mut *setup)
            .await?;
        }
        for (key, value) in &case.state {
            sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
                .bind(key)
                .bind(value.to_string())
                .execute(&mut *setup)
                .await?;
        }
        setup.commit().await?;
        let expected = match case.expected.as_str() {
            "WaitingForSync" => DiaryContextAvailability::WaitingForSync,
            "Withheld" => DiaryContextAvailability::Withheld,
            "Ready" => DiaryContextAvailability::Ready,
            _ => return Err("Unknown reference result".into()),
        };
        assert_eq!(
            database.diary_context_availability(now).await?,
            expected,
            "{}",
            case.name
        );
        let mut cleanup = pool.begin().await?;
        sqlx::query("DELETE FROM jobs WHERE starts_with(dedup_key,$1)")
            .bind(format!("context-gate:{}:", case.name))
            .execute(&mut *cleanup)
            .await?;
        for key in case.state.keys() {
            sqlx::query("DELETE FROM app_state WHERE key=$1")
                .bind(key)
                .execute(&mut *cleanup)
                .await?;
        }
        cleanup.commit().await?;
    }
    Ok(())
}
