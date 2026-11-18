use std::{env, error::Error, fs};

use ga_db::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{ClarificationClock, InterpretationContextQuery},
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "requires migration/check_diary_interpretation_continuations.py on disposable TimescaleDB"]
async fn reads_confirmed_and_current_diary_continuations() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected test database URL")?;
    let options = format!("postgresql://{address}")
        .parse::<PgConnectOptions>()?
        .disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected test DB")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let now: StoredInstant = "2026-10-25T10:00:00+00:00".parse()?;
    let clock = ClarificationClock {
        message_at: now,
        conversation_now: now,
        use_message_time: false,
    };
    let binding = ConversationBinding {
        owner_id: owner,
        conversation_id: Uuid::from_u128(901),
    };
    let model = ShareDestination::new(DestinationKind::Model, "model:primary")?;
    let include = || {
        database.interpretation_context(InterpretationContextQuery {
            binding,
            model,
            now,
            clarification_clock: clock,
            mentioned_events: &[],
        })
    };
    let conversation = || {
        sqlx::query_scalar::<_, String>("SELECT state::text FROM conversations WHERE id=$1")
            .bind(binding.conversation_id)
            .fetch_one(&pool)
    };
    let before = conversation().await?;
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_CONTEXT")?)?)?;
    let got = include().await?;
    let actual = json!({"recent_events":got.events.entries,"history_truncated":got.events.history_truncated,
        "recent_questions":got.recent_questions,"pending_clarification":got.pending_clarification,
        "recent_analysis_question":got.recent_analysis_question});
    assert_eq!(actual, fixture);
    assert_eq!(got.channel, "telegram:allowed");
    assert_eq!(conversation().await?, before);

    sqlx::query(
        "UPDATE app_state SET value=value || '{\"status\":\"uncertain\"}'::jsonb
        WHERE key='outbox:update:802:0'",
    )
    .execute(&pool)
    .await?;
    let got = include().await?;
    let previous = got
        .recent_analysis_question
        .ok_or("Expected a confirmed question")?;
    assert_eq!(previous["question"], "Older native question");

    sqlx::query(
        "DELETE FROM app_state WHERE key LIKE 'tracker-consent:%:channel:telegram:allowed'",
    )
    .execute(&pool)
    .await?;
    let got = include().await?;
    assert!(got.pending_clarification.is_none());
    assert_eq!(got.recent_questions.len(), 1);
    assert_eq!(
        got.recent_questions[0]["id"],
        Uuid::from_u128(830).to_string()
    );
    assert!(
        got.events
            .entries
            .iter()
            .all(|event| event["id"] != Uuid::from_u128(1).to_string())
    );
    assert_eq!(conversation().await?, before);
    println!(
        "Verified reference questions and form, same-channel delivery, fallback to confirmed native memory, and read-only privacy checks"
    );
    Ok(())
}
