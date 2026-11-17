use std::{env, error::Error, io, str::FromStr};

use ga_db::{
    Database,
    diary::{DeleteEvent, DiaryWriteError, UndoDiaryOperation},
};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

async fn by_key(inspect: &PgPool, key: &str) -> Result<Uuid, sqlx::Error> {
    sqlx::query_scalar("SELECT id FROM events WHERE idempotency_key = $1")
        .bind(key)
        .fetch_one(inspect)
        .await
}

#[tokio::test]
#[ignore = "uses Python's historical audits on a freshly migrated disposable TimescaleDB"]
async fn reads_python_audits_and_rolls_back_an_entire_stale_group() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, expected);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&inspect)
        .await?;

    let undo = |actor| UndoDiaryOperation {
        owner_id: owner,
        authenticated_actor: actor,
    };
    let delete = |id, revision| DeleteEvent {
        owner_id: owner,
        authenticated_actor: "synthetic-delete",
        event_id: id,
        expected_revision: revision,
    };
    assert!(matches!(
        db.undo_diary_operation(undo("synthetic-unknown")).await,
        Err(DiaryWriteError::NothingToUndo)
    ));

    let episode = by_key(&inspect, "synthetic-migraine-group").await?;
    let symptom = by_key(&inspect, "synthetic-symptom-group").await?;
    assert!(matches!(
        db.delete_event(delete(episode, 0)).await,
        Err(DiaryWriteError::RevisionChanged)
    ));
    assert!(matches!(
        db.delete_event(delete(episode, 1)).await,
        Err(DiaryWriteError::LinkedObservations)
    ));
    let group = db
        .undo_diary_operation(undo("telegram:synthetic-group"))
        .await?;
    assert_eq!(
        (group.id, group.revision, group.deleted, group.count),
        (symptom, 2, true, 2)
    );
    assert!(matches!(
        db.undo_diary_operation(undo("telegram:synthetic-group"))
            .await,
        Err(DiaryWriteError::UndoConflict)
    ));

    assert!(matches!(
        db.undo_diary_operation(undo("telegram:synthetic-stale"))
            .await,
        Err(DiaryWriteError::UndoConflict)
    ));
    let unmodified_id = by_key(&inspect, "synthetic-stale-second").await?;
    let unchanged: (i32, bool) =
        sqlx::query_as("SELECT revision, deleted FROM events WHERE id = $1")
            .bind(unmodified_id)
            .fetch_one(&inspect)
            .await?;
    assert_eq!(unchanged, (1, false));
    let audits: i64 = sqlx::query_scalar("SELECT count(*) FROM audit_log WHERE action = 'undo' AND actor = 'telegram:synthetic-stale'")
        .fetch_one(&inspect).await?;
    assert_eq!(audits, 0);

    let legacy_id = by_key(&inspect, "synthetic-legacy-envelope").await?;
    let legacy = db.undo_diary_operation(undo("mcp")).await?;
    assert_eq!(
        (legacy.id, legacy.revision, legacy.deleted),
        (legacy_id, 3, false)
    );

    let hidden = by_key(&inspect, "user.synthetic_hidden").await?;
    let forbidden = by_key(&inspect, "user.synthetic_keep").await?;
    assert!(matches!(
        db.delete_event(delete(forbidden, 1)).await,
        Err(DiaryWriteError::DeletionForbidden)
    ));
    let redacted = db.delete_event(delete(hidden, 1)).await?;
    assert_eq!(
        redacted.snapshot,
        json!({"id": hidden.to_string(), "revision": 2, "deleted": true})
    );
    let private = db.undo_diary_operation(undo("synthetic-delete")).await?;
    assert_eq!(
        (private.id, private.revision, private.deleted, private.count),
        (hidden, 3, false, 1)
    );
    Ok(())
}
