use std::{env, error::Error, io, str::FromStr};

use ga_db::{
    Database,
    diary::custom_registry::{CustomRegistryError, ManageCustomDefinition},
};
use ga_domain::event_schema::CustomDefinition;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses disposable TimescaleDB seeded with a tracker from pinned Python"]
async fn revises_an_existing_tracker_and_atomically_retires_reminders() -> Result<(), Box<dyn Error>>
{
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
    let (id, owner, revision): (Uuid, Uuid, i32) = sqlx::query_as(
        "SELECT id,owner_id,revision FROM event_definitions WHERE key='user.synthetic_tracker'",
    )
    .fetch_one(&inspect)
    .await?;
    let request = ManageCustomDefinition {
        owner_id: owner,
        authenticated_actor: "synthetic-admin",
        may_manage_definitions: true,
    };
    let raw: Value = serde_json::from_str(&env::var("GA_TEST_TRACKER_SPEC")?)?;
    let spec = CustomDefinition::from_json(&raw)?;
    assert!(matches!(
        db.propose_custom_definition(
            ManageCustomDefinition {
                may_manage_definitions: false,
                ..request
            },
            id,
            revision,
            &spec,
        )
        .await,
        Err(CustomRegistryError::Forbidden)
    ));
    assert!(matches!(
        db.propose_custom_definition(request, id, revision - 1, &spec)
            .await,
        Err(CustomRegistryError::RevisionChanged)
    ));
    let mut changed = raw.clone();
    changed["fields"]["rating"]["id"] = json!("user.renamed_rating");
    assert!(matches!(
        db.propose_custom_definition(
            request,
            id,
            revision,
            &CustomDefinition::from_json(&changed)?
        )
        .await,
        Err(CustomRegistryError::FieldIdentityChanged)
    ));
    let mut invalid = raw.clone();
    invalid["fields"]["rating"]["unit"] = json!("broken");
    let pending = db
        .propose_custom_definition(
            request,
            id,
            revision,
            &CustomDefinition::from_json(&invalid)?,
        )
        .await?;
    assert!(matches!(
        db.activate_custom_definition(request, id, pending.revision)
            .await,
        Err(CustomRegistryError::Metric)
    ));
    let remaining: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM event_definition_versions WHERE definition_id=$1),
                (SELECT count(*) FROM metric_definition_versions)",
    )
    .bind(id)
    .fetch_one(&inspect)
    .await?;
    assert_eq!(
        remaining,
        (1, 4),
        "failed activation leaves no partial versions"
    );
    let proposed = db
        .propose_custom_definition(request, id, pending.revision, &spec)
        .await?;
    let active = db
        .activate_custom_definition(request, id, proposed.revision)
        .await?;
    assert_eq!(active.version, 2);
    let retired = db
        .retire_custom_definition(request, id, active.new_definition_revision)
        .await?;
    assert_eq!(retired.status, "retired");
    assert_eq!(retired.current_version, Some(2));

    let mut draft = raw;
    draft["key"] = json!("user.synthetic_fresh_definition");
    let spec = CustomDefinition::from_json(&draft)?;
    let recorded = db.draft_custom_definition(request, &spec).await?;
    assert!(matches!(
        db.draft_custom_definition(request, &spec).await,
        Err(CustomRegistryError::KeyTaken)
    ));
    assert_eq!(recorded.revision, 1);
    let activated = db
        .activate_custom_definition(request, recorded.id, recorded.revision)
        .await?;
    assert_eq!(activated.version, 1);
    Ok(())
}
