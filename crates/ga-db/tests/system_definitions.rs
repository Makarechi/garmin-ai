use std::{env, error::Error, io, str::FromStr};

use ga_db::{
    Database,
    system_definitions::{DefinitionStoreError, SystemDefinition},
};
use ga_domain::event_provenance::EventTopology;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses a migrated disposable TimescaleDB fixture"]
async fn reuses_python_definitions_and_preserves_old_versions() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, name);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let before = active_versions(&inspect).await?;
    assert!(before.is_empty() || before.len() == 19);
    let installed = db.install_system_definitions().await?;
    let after = active_versions(&inspect).await?;
    assert_eq!(installed.len(), 19);
    assert_eq!(after.len(), 19);
    if !before.is_empty() {
        assert_eq!(before, after);
    }
    assert_eq!(db.install_system_definitions().await?, installed);

    let contracts: Value = serde_json::from_str(include_str!("../contracts/system-events.json"))?;
    for record in contracts
        .as_array()
        .ok_or("Expected bundled system contracts")?
    {
        let kind = record["kind"].as_str().ok_or("Expected kind")?;
        let key = format!("system.{kind}");
        let row = db.ensure_system_definition(kind).await?;
        let expected = installed
            .iter()
            .find(|entry| entry.0 == kind)
            .ok_or("Missing installed kind")?
            .1;
        assert_eq!(row, expected);
        let stored: (String, String, String, String, String, String) = sqlx::query_as(
            "SELECT schema_hash, schema::text, field_metadata::text, labels::text,
                privacy, allowed_operations::text FROM event_definition_versions WHERE id = $1",
        )
        .bind(row.id)
        .fetch_one(&inspect)
        .await?;
        let stored_id = after
            .iter()
            .find(|entry| entry.0 == key)
            .ok_or("Missing stored kind")?
            .1;
        assert_eq!(stored_id, row.id);
        assert_eq!(stored.0, record["hash"]);
        assert_eq!(
            serde_json::from_str::<Value>(&stored.1)?,
            record["contract"]["schema"]
        );
        assert_eq!(
            serde_json::from_str::<Value>(&stored.2)?,
            record["contract"]["fields"]
        );
        assert_eq!(
            serde_json::from_str::<Value>(&stored.3)?,
            record["contract"]["labels"]
        );
        assert_eq!(stored.4, record["contract"]["privacy"]);
        assert_eq!(
            serde_json::from_str::<Value>(&stored.5)?,
            record["contract"]["allowed_operations"]
        );
    }

    let unchanged = db.ensure_system_definition("note").await?;
    assert_eq!(unchanged.version, 1);
    let earlier_schema = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO event_definition_versions (id, definition_id, version, schema, schema_hash, topology,
             field_metadata, labels, privacy, allowed_operations)
         SELECT $1, definition_id, 2, schema, 'synthetic-older-contract', topology,
             field_metadata, labels, privacy, allowed_operations
         FROM event_definition_versions WHERE id = $2",
    )
    .bind(earlier_schema).bind(unchanged.id).execute(&inspect).await?;
    sqlx::query(
        "UPDATE event_definitions SET current_version = 2, revision = 2 WHERE key = 'system.note'",
    )
    .execute(&inspect)
    .await?;
    let refreshed = db.ensure_system_definition("note").await?;
    assert_eq!(
        refreshed,
        SystemDefinition {
            id: refreshed.id,
            version: 3,
            topology: EventTopology::Flexible
        }
    );
    assert_ne!(refreshed.id, unchanged.id);
    assert_ne!(refreshed.id, earlier_schema);
    let versions: Vec<Uuid> = sqlx::query_scalar(
        "SELECT version.id FROM event_definitions AS definition
         JOIN event_definition_versions AS version ON version.definition_id = definition.id
         WHERE definition.key = 'system.note' ORDER BY version.version",
    )
    .fetch_all(&inspect)
    .await?;
    assert_eq!(versions, [unchanged.id, earlier_schema, refreshed.id]);
    assert_eq!(db.ensure_system_definition("note").await?, refreshed);
    assert_eq!(
        db.ensure_system_definition("custom.synthetic").await,
        Err(DefinitionStoreError::UnknownKind)
    );
    Ok(())
}

async fn active_versions(db: &PgPool) -> Result<Vec<(String, Uuid, i32, String)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT definition.key, version.id, definition.revision, version.schema_hash
         FROM event_definitions AS definition
         JOIN event_definition_versions AS version
           ON version.definition_id = definition.id AND version.version = definition.current_version
         WHERE definition.namespace = 'system' ORDER BY definition.key",
    )
    .fetch_all(db)
    .await
}
