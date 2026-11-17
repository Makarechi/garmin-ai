use std::{env, error::Error, io, str::FromStr};

use ga_db::Database;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB with or without existing Python contracts"]
async fn reuses_existing_metric_versions_and_retains_superseded_rows() -> Result<(), Box<dyn Error>>
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
    let old: Vec<(String, Uuid)> = sqlx::query_as(
        "SELECT definition.key, contract.id FROM metric_definitions AS definition
         JOIN metric_definition_versions AS contract ON contract.definition_id = definition.id
             AND contract.version = definition.current_version WHERE definition.namespace = 'system'",
    )
    .fetch_all(&inspect)
    .await?;
    let versions = db.ensure_system_metrics().await?;
    assert_eq!(versions.len(), 11);
    for (key, id) in old {
        let short = key
            .strip_prefix("system.")
            .ok_or("Invalid synthetic registry")?;
        assert_eq!(
            versions.get(short).ok_or("Expected existing contract")?.id,
            id
        );
    }
    let previous = *versions
        .get("heart_rate_bpm")
        .ok_or("Missing heart-rate contract")?;
    let obsolete: Uuid = sqlx::query_scalar(
        "WITH past AS (
             INSERT INTO metric_definition_versions (id, definition_id, version, schema_hash,
                 value_kind, unit, dimension, scale_id, scale_version, aggregation, coverage_policy,
                 time_semantics, minimum, maximum, labels, allowed_methods, category_domain)
             SELECT $2, definition_id, version + 1, 'synthetic-prior-contract', value_kind,
                 unit, dimension, scale_id, scale_version, aggregation, coverage_policy,
                 time_semantics, minimum, maximum, labels, allowed_methods, category_domain
             FROM metric_definition_versions WHERE id = $1
             RETURNING id, definition_id, version
         ) UPDATE metric_definitions AS definition SET current_version = past.version
           FROM past WHERE definition.id = past.definition_id RETURNING past.id",
    )
    .bind(previous.id)
    .bind(Uuid::new_v4())
    .fetch_one(&inspect)
    .await?;
    let latest = db.ensure_system_metrics().await?;
    let updated = latest
        .get("heart_rate_bpm")
        .ok_or("Missing updated contract")?;
    assert_ne!(updated.id, previous.id);
    assert_ne!(updated.id, obsolete);
    assert_eq!(updated.version, previous.version + 2);
    let historical: String =
        sqlx::query_scalar("SELECT schema_hash FROM metric_definition_versions WHERE id = $1")
            .bind(obsolete)
            .fetch_one(&inspect)
            .await?;
    assert_eq!(historical, "synthetic-prior-contract");
    assert_eq!(latest, db.ensure_system_metrics().await?);
    Ok(())
}
