use std::{env, error::Error, io, str::FromStr};

use ga_db::{Database, system_metrics::MetricRegistryError};
use ga_domain::metric_contract::UserMetricContract;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

const KEY: &str = "user.synthetic_metric";

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB with a user metric installed by pinned Python"]
async fn updates_owned_metric_contract_without_rewriting_previous_versions()
-> Result<(), Box<dyn Error>> {
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
    let original: (Uuid, Uuid) = sqlx::query_as(
        "SELECT definition.owner_id, version.id FROM metric_definitions AS definition
         JOIN metric_definition_versions AS version ON version.definition_id = definition.id
            AND version.version = definition.current_version WHERE definition.key = $1",
    )
    .bind(KEY)
    .fetch_one(&inspect)
    .await?;
    let mut contract: Value = serde_json::from_str(&env::var("GA_TEST_PYTHON_USER_METRIC")?)?;
    let parsed = UserMetricContract::from_json(&contract)?;
    assert!(matches!(
        db.register_user_metric(original.0, false, &parsed).await,
        Err(MetricRegistryError::Forbidden)
    ));
    assert!(matches!(
        db.register_user_metric(Uuid::new_v4(), true, &parsed).await,
        Err(MetricRegistryError::OwnerMissing)
    ));
    assert_eq!(
        db.register_user_metric(original.0, true, &parsed).await?.id,
        original.1
    );
    contract["maximum"] = Value::from(201);
    let changed = UserMetricContract::from_json(&contract)?;
    let new = db.register_user_metric(original.0, true, &changed).await?;
    assert_eq!(new.version, 2);
    assert_ne!(new.id, original.1);
    assert_eq!(
        new,
        db.register_user_metric(original.0, true, &changed).await?
    );
    let old_max: Option<f64> =
        sqlx::query_scalar("SELECT maximum FROM metric_definition_versions WHERE id = $1")
            .bind(original.1)
            .fetch_one(&inspect)
            .await?;
    assert_eq!(old_max, Some(200.0));
    Ok(())
}
