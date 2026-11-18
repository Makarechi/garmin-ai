use std::{env, error::Error, fs};

use ga_db::{
    Database,
    proactive_control::{GeneratedDiaryQuestions, ProactiveControlError},
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::postgres::PgConnectOptions;
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    owner: String,
    waiting: String,
    now: String,
    questions: Vec<Value>,
    generation: Value,
}

#[tokio::test]
#[ignore = "requires migration/check_diary_question_generation.py and its disposable database"]
async fn generates_only_current_diary_questions() -> Result<(), Box<dyn Error>> {
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
        serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_QUESTIONS")?)?)?;
    let owner: Uuid = fixture.owner.parse()?;
    let now: StoredInstant = fixture.now.parse()?;
    let minutes = |minutes| (OffsetDateTime::from(now) + Duration::minutes(minutes)).try_into();

    let mut concurrent = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *concurrent)
        .await?;
    assert!(matches!(
        database.generate_diary_questions(owner, now, "UTC").await,
        Err(ProactiveControlError::Busy)
    ));
    concurrent.rollback().await?;
    assert_eq!(
        database.generate_diary_questions(owner, now, "UTC").await?,
        GeneratedDiaryQuestions {
            migraines: 2,
            caffeine: 1,
            context: 0,
        },
    );
    assert_eq!(
        database.generate_diary_questions(owner, now, "UTC").await?,
        GeneratedDiaryQuestions::default(),
    );
    let questions: Vec<(String, OffsetDateTime, OffsetDateTime)> = sqlx::query_as(
        "SELECT jsonb_build_object(
            'kind',kind,'key',dedup_key,'text',text,'evidence',evidence,'priority',priority,
            'status',status,'attempts',attempts,'event_id',event_id,'sent_at',sent_at)::text,
            earliest_send_at,expires_at FROM pending_questions ORDER BY dedup_key",
    )
    .fetch_all(&pool)
    .await?;
    let mut compared = Vec::new();
    for (raw, earliest, expires) in questions {
        let mut fields: Value = serde_json::from_str(&raw)?;
        fields["earliest"] = json!(StoredInstant::try_from(earliest)?.to_string());
        fields["expires"] = json!(StoredInstant::try_from(expires)?.to_string());
        compared.push(fields);
    }
    assert_eq!(compared, fixture.questions);

    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('proactive:enabled','{\"enabled\":false}')",
    )
    .execute(&pool)
    .await?;
    assert_eq!(
        database
            .generate_diary_questions(owner, minutes(30)?, "UTC")
            .await?,
        GeneratedDiaryQuestions::default(),
    );
    let generation: String =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='proactive:generation'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(
        serde_json::from_str::<Value>(&generation)?,
        fixture.generation
    );

    sqlx::query("UPDATE app_state SET value='{\"enabled\":true}' WHERE key='proactive:enabled'")
        .execute(&pool)
        .await?;
    sqlx::query("UPDATE module_configs SET reminders_enabled=false WHERE pack_key='migraine'")
        .execute(&pool)
        .await?;
    assert_eq!(
        database
            .generate_diary_questions(owner, minutes(30)?, "UTC")
            .await?,
        GeneratedDiaryQuestions::default(),
    );
    sqlx::query("UPDATE module_configs SET reminders_enabled=true WHERE pack_key='migraine'")
        .execute(&pool)
        .await?;
    assert_eq!(
        database
            .generate_diary_questions(owner, minutes(60)?, "UTC")
            .await?,
        GeneratedDiaryQuestions {
            migraines: 1,
            caffeine: 0,
            context: 0,
        },
    );
    let waiting: Uuid = fixture.waiting.parse()?;
    let visible: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM pending_questions WHERE event_id=$1 AND status='pending')",
    )
    .bind(waiting)
    .fetch_one(&pool)
    .await?;
    assert!(visible);
    assert_eq!(
        database
            .generate_diary_questions(owner, minutes(1440)?, "UTC")
            .await?,
        GeneratedDiaryQuestions::default(),
    );
    println!(
        "Verified diary generation, deduplication, pause, pack changes, and covered caffeine day"
    );
    Ok(())
}
