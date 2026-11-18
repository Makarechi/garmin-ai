use std::{env, error::Error, fs};

use ga_db::{
    Database,
    diary::{DiaryReadError, InterpretationEventsQuery},
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    now: String,
    model: String,
    channel: String,
    rows: Vec<Value>,
    truncated: bool,
}

#[tokio::test]
#[ignore = "requires migration/check_diary_interpretation_events.py on disposable TimescaleDB"]
async fn matches_model_visible_history_and_checks_named_entries() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let suffix = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected test database URL")?;
    let options = format!("postgresql://{suffix}")
        .parse::<PgConnectOptions>()?
        .disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable test DB")?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, target);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let owner = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&inspect)
        .await?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_CONTEXT")?)?)?;
    assert_eq!(cases.len(), 4);
    for case in cases {
        let read = db
            .interpretation_events(InterpretationEventsQuery {
                owner,
                model: ShareDestination::new(DestinationKind::Model, &case.model)?,
                channel: Some(ShareDestination::new(
                    DestinationKind::Channel,
                    &case.channel,
                )?),
                now: case.now.parse()?,
                mentioned: &[],
            })
            .await?;
        assert_eq!(read.entries, case.rows);
        assert_eq!(read.history_truncated, case.truncated);
        assert_eq!(read.channel_versions, read.tracker_versions);
        assert!(
            read.entries
                .iter()
                .all(|entry| entry.get("original_text").is_none())
        );
    }
    let now: StoredInstant = "2026-10-25T10:00:00+00:00".parse()?;
    let model = ShareDestination::new(DestinationKind::Model, "model:primary")?;
    let allowed = ShareDestination::new(DestinationKind::Channel, "telegram:allowed")?;
    let schema_only = ShareDestination::new(DestinationKind::Channel, "telegram:schema")?;
    let explicit = [
        Uuid::from_u128(120),
        Uuid::from_u128(1),
        Uuid::from_u128(120),
    ];
    let query = InterpretationEventsQuery {
        owner,
        model,
        channel: Some(allowed),
        now,
        mentioned: &explicit,
    };
    let read = db
        .interpretation_events(InterpretationEventsQuery { ..query })
        .await?;
    assert_eq!(read.entries.len(), 2);
    assert_eq!(read.entries[0]["id"], explicit[0].to_string());
    assert_eq!(read.entries[1]["id"], explicit[1].to_string());
    assert!(!read.history_truncated);
    assert_eq!(read.tracker_versions.len(), 1);
    let missing = [Uuid::nil()];
    for (channel, ids) in [
        (schema_only, explicit.as_slice()),
        (allowed, missing.as_slice()),
    ] {
        assert!(matches!(
            db.interpretation_events(InterpretationEventsQuery {
                channel: Some(channel),
                mentioned: ids,
                ..query
            })
            .await,
            Err(DiaryReadError::ContextChanged)
        ));
    }
    sqlx::query(
        "UPDATE app_state SET value=jsonb_build_object('model_categories','[\"health\"]'::jsonb)
        WHERE key='preferences:onboarding'",
    )
    .execute(&inspect)
    .await?;
    assert!(matches!(
        db.interpretation_events(query).await,
        Err(DiaryReadError::ModelConsent)
    ));
    println!(
        "Verified Python model context, explicit record order, missing and denied references, and live owner settings"
    );
    Ok(())
}
