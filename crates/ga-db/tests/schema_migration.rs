use std::{env, error::Error, str::FromStr};

use ga_db::{
    Database,
    schema_migration::{SchemaMigrationError, SchemaState},
};
use sqlx::{ConnectOptions, Connection, PgConnection, postgres::PgConnectOptions};

#[tokio::test]
#[ignore = "requires one disposable PostgreSQL database per migration source"]
async fn migrates_only_a_supported_disposable_schema() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Unsupported fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected_name = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("The fixture must use a disposable test database")?;
    let mut inspect = PgConnection::connect_with(&options).await?;
    let name: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&mut inspect)
        .await?;
    assert_eq!(name, expected_name);
    let database =
        Database::connect_lazy(&url).map_err(|_| "Invalid fixture database configuration")?;
    match env::var("GA_TEST_SCHEMA_CASE")?.as_str() {
        "unknown" => {
            assert!(matches!(
                database.schema_status().await,
                Err(SchemaMigrationError::Unsupported)
            ));
            assert!(matches!(
                database.migrate_schema().await,
                Err(SchemaMigrationError::Unsupported)
            ));
        }
        "occupied" => {
            assert!(matches!(
                database.schema_status().await,
                Err(SchemaMigrationError::Occupied)
            ));
            assert!(matches!(
                database.migrate_schema().await,
                Err(SchemaMigrationError::Occupied)
            ));
        }
        "telegram_collision" => {
            assert_eq!(
                database.schema_status().await?,
                SchemaState::BeforeTelegramRepair
            );
            assert!(matches!(
                database.migrate_schema().await,
                Err(SchemaMigrationError::ConversationCollision)
            ));
            assert_eq!(
                database.schema_status().await?,
                SchemaState::BeforeTelegramRepair
            );
        }
        case @ ("rollback" | "rollback_ingestion") => {
            let expected = if case == "rollback_ingestion" {
                SchemaState::BeforeIngestionTime
            } else {
                SchemaState::BeforeRevisions
            };
            assert_eq!(database.schema_status().await?, expected);
            assert!(database.migrate_schema().await.is_err());
            assert_eq!(database.schema_status().await?, expected);
            let table: bool = sqlx::query_scalar(
                "SELECT to_regclass('public.measurement_revisions') IS NOT NULL",
            )
            .fetch_one(&mut inspect)
            .await?;
            assert!(!table);
            if case == "rollback_ingestion" {
                let columns: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT FROM information_schema.columns WHERE table_schema='public'
                     AND ((table_name='measurements' AND column_name='ingested_at') OR
                          (table_name='metric_definition_versions' AND column_name='category_domain')))",
                )
                .fetch_one(&mut inspect)
                .await?;
                assert!(!columns);
            }
        }
        other => {
            let expected = match other {
                "fresh" => SchemaState::Empty,
                "before_telegram_repair" => SchemaState::BeforeTelegramRepair,
                "before_ingestion" => SchemaState::BeforeIngestionTime,
                "before_revisions" => SchemaState::BeforeRevisions,
                "before_fence" => SchemaState::BeforeFence,
                _ => return Err("Unrecognized fixture case".into()),
            };
            assert_eq!(database.schema_status().await?, expected);
            let mut running = inspect.begin().await?;
            sqlx::query("SELECT pg_advisory_xact_lock(72104620)")
                .execute(&mut *running)
                .await?;
            assert!(matches!(
                database.migrate_schema().await,
                Err(SchemaMigrationError::Busy)
            ));
            running.rollback().await?;
            assert_eq!(database.migrate_schema().await?, expected);
            assert_eq!(database.schema_status().await?, SchemaState::Current);
            assert_eq!(database.migrate_schema().await?, SchemaState::Current);
        }
    }
    Ok(())
}
