use std::{env, error::Error, fs, future::ready};

use ga_db::{
    Database,
    outbox_claims::ReplyDestination,
    proactive_control::{
        DiaryQuestionKind, DiaryQuestionOutcome, GeneratedDiaryQuestions, NotificationExclusions,
        NotificationPolicy, QuestionDispatchResult,
    },
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime, Time};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    owner: String,
    now: String,
    generated: Value,
    reused: Value,
    selected: Value,
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

    start_replay(&pool).await?;
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
    verify_dispatch(&database, &pool, owner, minutes(60)?, &fixture.selected).await?;
    println!(
        "Verified Python context generation and selection, followed by current evidence checks at dispatch"
    );
    Ok(())
}

async fn start_replay(pool: &PgPool) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,parser_version,status,payload)
         VALUES(gen_random_uuid(),'garmin_connect','daily','2026-10-25',repeat('a',64),'synthetic',0,'pending','{}')",
    ).execute(pool).await?;
    Ok(())
}

async fn verify_dispatch(
    database: &Database,
    pool: &PgPool,
    owner: Uuid,
    now: StoredInstant,
    selected: &Value,
) -> Result<(), Box<dyn Error>> {
    sqlx::raw_sql(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         SELECT gen_random_uuid(),id,'telegram','fixture','42','fixture' FROM people;
         UPDATE app_state SET value='{\"channel\":{\"channel\":\"telegram\",\"instance_id\":\"fixture\"}}'
            WHERE key='preferences:onboarding';",
    ).execute(pool).await?;
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "telegram",
        instance_id: "fixture",
    };
    let policy = NotificationPolicy {
        timezone: "UTC",
        default_enabled: true,
        daily_budget: Some(2),
        quiet_start: Time::MIDNIGHT,
        quiet_end: Time::MIDNIGHT,
        snoozed_until: None,
        destination: None,
        exclusions: NotificationExclusions::default(),
    };
    assert!(
        database
            .reserve_diary_question(owner, &policy, now)
            .await?
            .is_none()
    );
    let reserve = || database.reserve_diary_question_with_context(owner, &policy, now);
    let question = reserve().await?.ok_or("Missing current context question")?;
    assert_eq!(question.kind, DiaryQuestionKind::Context);
    assert_eq!(&snapshot(pool).await?, selected);

    start_replay(pool).await?;
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &question, &policy, now, |_| never_send())
            .await?,
        QuestionDispatchResult::Unavailable
    );
    assert_eq!(snapshot(pool).await?["status"], "pending");
    assert!(reserve().await?.is_none());
    sqlx::query("DELETE FROM source_payloads WHERE archive_key='synthetic'")
        .execute(pool)
        .await?;

    let question = reserve().await?.ok_or("Missing resumed context question")?;
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES(gen_random_uuid(),'garmin_endpoint','{\"endpoint\":\"heart_rate\"}',
                  'synthetic-context-refresh','pending',$1,0)",
    )
    .bind(OffsetDateTime::from(now))
    .execute(pool)
    .await?;
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &question, &policy, now, |_| never_send())
            .await?,
        QuestionDispatchResult::Unavailable,
    );
    assert_eq!(snapshot(pool).await?["status"], "pending");
    assert!(reserve().await?.is_none());
    sqlx::query("DELETE FROM jobs WHERE dedup_key='synthetic-context-refresh'")
        .execute(pool)
        .await?;
    let question = reserve().await?.ok_or("Missing context after fresh sync")?;
    let evidence = &selected["evidence"];
    let left: StoredInstant = evidence["start"]
        .as_str()
        .ok_or("Missing context start")?
        .parse()?;
    let right: StoredInstant = evidence["end"]
        .as_str()
        .ok_or("Missing context end")?
        .parse()?;
    add_context(pool, left.into(), right.into()).await?;
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &question, &policy, now, |_| never_send())
            .await?,
        QuestionDispatchResult::Cancelled
    );
    let cancelled = snapshot(pool).await?;
    assert_eq!(
        cancelled["evidence"]["context_coverage"]["uncovered_seconds"],
        0.0
    );
    assert_eq!(cancelled["status"], "cancelled");
    let marker = format!("outbox:question:{}:0", question.id);
    let attempt: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_state WHERE key=$1)")
        .bind(&marker)
        .fetch_one(pool)
        .await?;
    assert!(!attempt);

    sqlx::raw_sql(
        "DELETE FROM timeline_intervals WHERE id='synthetic-recent-context';
         DELETE FROM app_state WHERE key='proactive:generation';",
    )
    .execute(pool)
    .await?;
    assert_eq!(
        database
            .generate_diary_questions_with_context(owner, now, "UTC")
            .await?
            .context,
        1
    );
    let question = reserve()
        .await?
        .ok_or("Missing context after removing coverage")?;
    let partial_end = OffsetDateTime::from(left) + Duration::minutes(4);
    add_context(pool, left.into(), partial_end).await?;
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &question, &policy, now, |text| {
                assert!(text.contains("Неизвестный контекст: 14 мин; 25.10 23:55–26.10 00:09."));
                ready(DiaryQuestionOutcome::Accepted(417))
            })
            .await?,
        QuestionDispatchResult::Sent
    );
    let receipt: String =
        sqlx::query_scalar("SELECT value->>'message_id' FROM app_state WHERE key=$1")
            .bind(&marker)
            .fetch_one(pool)
            .await?;
    assert_eq!(receipt, "417");
    assert_eq!(snapshot(pool).await?["status"], "sent");
    assert!(reserve().await?.is_none());
    Ok(())
}

async fn add_context(
    pool: &PgPool,
    left: OffsetDateTime,
    right: OffsetDateTime,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO timeline_intervals(id,start,\"end\",source,label,confidence,confirmed,evidence)
         VALUES('synthetic-recent-context',$1,$2,'manual','Synthetic coverage',1,true,'{}')",
    ).bind(left).bind(right).execute(pool).await?;
    Ok(())
}

fn never_send() -> std::future::Ready<DiaryQuestionOutcome> {
    panic!("A context question without current evidence must not be sent");
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
