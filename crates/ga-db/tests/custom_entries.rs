use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database,
    diary::{
        DiaryWriteError,
        custom_entries::{CreateCustomEntry, UpdateCustomEntry},
    },
};
use ga_domain::{event_replay::ReplayConflict, event_schema::CustomEntry};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses disposable TimescaleDB with old and current Python custom definitions"]
async fn writes_custom_entries_against_the_right_recorded_version() -> Result<(), Box<dyn Error>> {
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
    let old: Uuid =
        sqlx::query_scalar("SELECT id FROM events WHERE idempotency_key='original-entry'")
            .fetch_one(&inspect)
            .await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let inputs: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_CUSTOM_ENTRIES")?)?)?;
    let actor = "synthetic-entry";
    let evidence = [json!({"kind":"synthetic","id":"one"})];

    let old_edit = CustomEntry::from_value(inputs["old_edit"].clone())?;
    let operation = UpdateCustomEntry {
        owner_id: owner,
        actor,
        id: old,
        expected_revision: 1,
        entry: &old_edit,
        evidence_refs: &evidence,
    };
    let updated = db.update_custom_entry(operation).await?;
    assert_eq!(updated.revision, 2);
    assert_eq!(
        updated.snapshot["payload"]["value"],
        inputs["old_edit"]["values"]["value"]
    );
    assert!(matches!(
        db.update_custom_entry(UpdateCustomEntry {
            owner_id: owner,
            actor,
            id: old,
            expected_revision: 1,
            entry: &old_edit,
            evidence_refs: &evidence,
        })
        .await,
        Err(DiaryWriteError::RevisionChanged)
    ));
    let original = CustomEntry::from_value(inputs["old_create"].clone())?;
    let retried = db
        .create_custom_entry(CreateCustomEntry {
            owner_id: owner,
            actor,
            entry: &original,
            idempotency_key: Some("original-entry"),
            evidence_refs: &[],
        })
        .await?;
    assert!(retried.reused);
    assert_eq!(retried.revision, 1);
    assert_eq!(
        retried.snapshot["payload"]["value"],
        inputs["old_create"]["values"]["value"]
    );

    let started = CustomEntry::from_value(inputs["create"].clone())?;
    let current = db
        .create_custom_entry(CreateCustomEntry {
            owner_id: owner,
            actor,
            entry: &started,
            idempotency_key: Some("new-entry"),
            evidence_refs: &evidence,
        })
        .await?;
    assert_eq!(
        current.snapshot["payload"]["marker"].to_string(),
        inputs["create"]["values"]["marker"].to_string()
    );
    let wrong = CustomEntry::from_value(inputs["adjacent_marker"].clone())?;
    assert!(matches!(
        db.create_custom_entry(CreateCustomEntry {
            owner_id: owner,
            actor,
            entry: &wrong,
            idempotency_key: Some("new-entry"),
            evidence_refs: &[],
        })
        .await,
        Err(DiaryWriteError::Replay(ReplayConflict::InputChanged))
    ));
    let wrong = CustomEntry::from_value(inputs["wrong_unit"].clone())?;
    assert!(matches!(
        db.create_custom_entry(CreateCustomEntry {
            owner_id: owner,
            actor,
            entry: &wrong,
            idempotency_key: None,
            evidence_refs: &[],
        })
        .await,
        Err(DiaryWriteError::CustomInput(_))
    ));
    Ok(())
}
