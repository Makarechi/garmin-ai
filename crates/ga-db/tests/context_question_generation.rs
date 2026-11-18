use std::{env, error::Error, fs};

use ga_db::{Database, proactive_control::GeneratedDiaryQuestions};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    owner: String,
    now: String,
    generated: Value,
    reused: Value,
}

#[tokio::test]
#[ignore = "requires migration/check_context_question_generation.py and its disposable database"]
async fn matches_context_generation_with_coverage_replay_and_reuse() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let suffix = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected test database URL")?;
    let options: PgConnectOptions = format!("postgresql://{suffix}").parse()?;
    if !options
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
    {
        return Err("Expected a disposable test database".into());
    }
    let pool = PgPool::connect_with(options).await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_QUESTIONS")?)?)?;
    let owner: Uuid = fixture.owner.parse()?;
    let now: StoredInstant = fixture.now.parse()?;
    let minutes = |n| (OffsetDateTime::from(now) + Duration::minutes(n)).try_into();

    sqlx::query("UPDATE module_configs SET tracking_enabled=false WHERE pack_key='general_diary'")
        .execute(&pool)
        .await?;
    assert_eq!(
        database
            .generate_diary_questions_with_context(owner, minutes(-30)?, "UTC")
            .await?,
        GeneratedDiaryQuestions::default()
    );
    sqlx::query("UPDATE module_configs SET tracking_enabled=true WHERE pack_key='general_diary'")
        .execute(&pool)
        .await?;

    sqlx::query(
        "INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,parser_version,status,payload)
         VALUES(gen_random_uuid(),'garmin_connect','daily','2026-10-25',repeat('a',64),'synthetic',0,'pending','{}')",
    ).execute(&pool).await?;
    assert_eq!(
        database
            .generate_diary_questions_with_context(owner, now, "UTC")
            .await?,
        GeneratedDiaryQuestions::default()
    );
    let completed: bool = sqlx::query_scalar(
        "SELECT value->'context_complete'='true'::jsonb FROM app_state WHERE key='proactive:generation'",
    ).fetch_one(&pool).await?;
    assert!(!completed);
    sqlx::query("DELETE FROM source_payloads WHERE archive_key='synthetic'")
        .execute(&pool)
        .await?;

    let result = database
        .generate_diary_questions_with_context(owner, now, "UTC")
        .await?;
    assert_eq!(result.context, 1);
    assert_eq!(snapshot(&pool).await?, fixture.generated);
    for offset in [0, 30] {
        assert_eq!(
            database
                .generate_diary_questions_with_context(owner, minutes(offset)?, "UTC")
                .await?,
            GeneratedDiaryQuestions::default()
        );
        assert_eq!(snapshot(&pool).await?, fixture.generated);
    }

    sqlx::raw_sql(
        "UPDATE pending_questions SET status='cancelled',
              evidence=evidence || '{\"cancel_reason\":\"owner_pause\"}'::jsonb WHERE kind='context';
         DELETE FROM app_state WHERE key='proactive:generation';",
    ).execute(&pool).await?;
    assert_eq!(
        database
            .generate_diary_questions_with_context(owner, minutes(30)?, "UTC")
            .await?,
        GeneratedDiaryQuestions::default()
    );
    assert_eq!(snapshot(&pool).await?["status"], "cancelled");
    sqlx::query(
        "UPDATE pending_questions SET evidence=evidence || '{\"cancel_reason\":\"synthetic-replay\"}'::jsonb WHERE kind='context'",
    ).execute(&pool).await?;
    let reused = database
        .generate_diary_questions_with_context(owner, minutes(60)?, "UTC")
        .await?;
    assert_eq!(reused.context, 1);
    assert_eq!(snapshot(&pool).await?, fixture.reused);
    println!(
        "Verified Python context evidence, owner time, overlap suppression, replay deferral and qualified reuse"
    );
    Ok(())
}

async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let rows: Vec<(String, OffsetDateTime, OffsetDateTime)> = sqlx::query_as(
        "SELECT jsonb_build_object('key',dedup_key,'text',text,'evidence',evidence,
                  'priority',priority,'status',status)::text,earliest_send_at,expires_at
         FROM pending_questions WHERE kind='context'",
    )
    .fetch_all(pool)
    .await?;
    let [(raw, earliest, expires)] = rows.as_slice() else {
        return Err("Expected exactly one context question".into());
    };
    let mut value: Value = serde_json::from_str(raw)?;
    value["earliest"] = json!(StoredInstant::try_from(*earliest)?.to_string());
    value["expires"] = json!(StoredInstant::try_from(*expires)?.to_string());
    Ok(value)
}
