use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database,
    diary::{DiaryReadError, ModelDiaryQuery},
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    start: String,
    end: String,
    model: String,
    channel: Option<String>,
    kind: Option<String>,
    limit: u16,
    expected: Value,
    tracker_versions: Vec<String>,
    channel_versions: Vec<String>,
}

#[tokio::test]
#[ignore = "uses the synthetic diary permissions fixture on disposable TimescaleDB"]
async fn model_sees_only_allowed_page_rows() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected disposable TimescaleDB URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Database must end in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(&actual, target);
    let owner_id = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&inspect)
        .await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_MODEL_DIARY")?)?)?;
    for (index, case) in cases.into_iter().enumerate() {
        let document = db
            .list_model_events(ModelDiaryQuery {
                owner_id,
                start: case.start.parse()?,
                end: case.end.parse()?,
                kind: case.kind.as_deref(),
                limit: case.limit,
                model: ShareDestination::new(DestinationKind::Model, &case.model)?,
                channel: case
                    .channel
                    .as_deref()
                    .map(|channel| ShareDestination::new(DestinationKind::Channel, channel))
                    .transpose()?,
            })
            .await?;
        assert_eq!(document.value, case.expected, "case {index}");
        let tracker_versions: Vec<_> = document
            .tracker_versions
            .iter()
            .map(|version| version.to_string())
            .collect();
        assert_eq!(tracker_versions, case.tracker_versions, "case {index}");
        let versions: Vec<_> = document
            .channel_versions
            .iter()
            .map(|version| version.to_string())
            .collect();
        assert_eq!(versions, case.channel_versions, "case {index}");
        assert!(!document.value.to_string().contains("synthetic quote"));
    }

    let start: StoredInstant = "2026-10-25T09:00:00+00:00".parse()?;
    let end: StoredInstant = "2026-10-25T10:00:00+00:00".parse()?;
    assert!(matches!(
        db.list_model_events(ModelDiaryQuery {
            owner_id: Uuid::nil(),
            start,
            end,
            kind: None,
            limit: 10,
            model: ShareDestination::new(DestinationKind::Model, "model:primary")?,
            channel: None,
        })
        .await,
        Err(DiaryReadError::Owner)
    ));

    sqlx::query(
        "UPDATE app_state SET value=jsonb_build_object('model_categories','[\"health\"]'::jsonb)
         WHERE key='preferences:onboarding'",
    )
    .execute(&inspect)
    .await?;
    assert!(matches!(
        db.list_model_events(ModelDiaryQuery {
            owner_id,
            start,
            end,
            kind: None,
            limit: 10,
            model: ShareDestination::new(DestinationKind::Model, "model:primary")?,
            channel: None,
        })
        .await,
        Err(DiaryReadError::ModelConsent)
    ));
    let owner = db
        .list_owner_events(start, end, Some("user.future"), 10)
        .await?;
    assert_eq!(owner["rows"].as_array().map(Vec::len), Some(10));
    assert_eq!(owner["truncated"], true);
    Ok(())
}
