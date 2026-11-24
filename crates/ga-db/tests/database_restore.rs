use std::{env, error::Error, fs, io::Cursor, str::FromStr};

use ga_db::{Database, database_restore::DatabaseRestoreError};
use serde_json::Value;
use sqlx::{ConnectOptions, Connection, PgConnection, postgres::PgConnectOptions};

#[tokio::test]
#[ignore = "requires the pinned synthetic export and a disposable restoration database"]
async fn restores_a_complete_snapshot_and_rolls_back_incomplete_input() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Unsupported test URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Only a disposable test database may be used")?;
    let mut inspect = PgConnection::connect_with(&options).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&mut inspect)
        .await?;
    assert_eq!(actual, name);
    let bootstrap: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='bootstrap:restore-test')",
    )
    .fetch_one(&mut inspect)
    .await?;
    assert!(
        bootstrap,
        "This test needs an isolated prepared bootstrap fixture"
    );

    let operation =
        Database::connect_lazy(&url).map_err(|_| "Test connection configuration failed")?;
    let fixture = fs::read_to_string(env::var("GA_TEST_RESTORE_INPUT")?)?;
    let header: Value = serde_json::from_str(fixture.lines().next().ok_or("Missing header")?)?;
    let fixture_revision = header["revision"].as_str().ok_or("Missing revision")?;
    assert!(matches!(fixture_revision, "c54f8a72d130" | "d1f7a9306e42"));
    let (rows, footer) = fixture
        .trim_end()
        .rsplit_once('\n')
        .ok_or("Missing fixture footer")?;
    let mut wrong_counts: Value = serde_json::from_str(footer)?;
    wrong_counts["counts"]["events"] = 0.into();
    let wrong_footer = format!("{rows}\n{wrong_counts}\n");
    let malformed_json = fixture.replacen("\"revision\": 2", "\"revision\": NaN", 1);
    assert_ne!(malformed_json, fixture);
    let missing_predecessor = fixture.replacen(
        "\"supersedes_id\": \"00000000-0000-0000-0000-000000000322\"",
        "\"supersedes_id\": \"00000000-0000-0000-0000-000000000999\"",
        1,
    );
    assert_ne!(missing_predecessor, fixture);

    for lock in [72_104_620_i64, 72_104_622, 72_104_625] {
        let mut active = inspect.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock($1)")
            .bind(lock)
            .execute(&mut *active)
            .await?;
        assert!(matches!(
            operation
                .restore_current_database(&mut Cursor::new(&fixture))
                .await,
            Err(DatabaseRestoreError::Busy)
        ));
        active.rollback().await?;
    }

    let invalid = [
        (fixture.replacen(fixture_revision, "unknown", 1), true),
        (format!("{rows}\n"), false),
        (wrong_footer, false),
        (malformed_json, false),
        (missing_predecessor, false),
        (format!("{fixture}\n{{}}\n"), false),
    ];
    for (input, revision) in invalid {
        let Err(error) = operation
            .restore_current_database(&mut Cursor::new(input))
            .await
        else {
            return Err("Invalid fixture restored".into());
        };
        assert!(
            if revision {
                matches!(error, DatabaseRestoreError::Revision)
            } else {
                matches!(error, DatabaseRestoreError::InvalidExport)
            },
            "{error}"
        );
        let unchanged: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='bootstrap:restore-test')
             AND NOT EXISTS(SELECT 1 FROM events) AND NOT EXISTS(SELECT 1 FROM health_days)
             AND NOT EXISTS(SELECT 1 FROM inbound_messages)",
        )
        .fetch_one(&mut inspect)
        .await?;
        assert!(unchanged);
    }

    sqlx::query("INSERT INTO app_state(key,value) VALUES ('test:existing-user-data','{}')")
        .execute(&mut inspect)
        .await?;
    assert!(matches!(
        operation
            .restore_current_database(&mut Cursor::new(&fixture))
            .await,
        Err(DatabaseRestoreError::NotEmpty)
    ));
    sqlx::query("DELETE FROM app_state WHERE key='test:existing-user-data'")
        .execute(&mut inspect)
        .await?;

    let counts = operation
        .restore_current_database(&mut Cursor::new(&fixture))
        .await?;
    assert_eq!(counts.len(), 30);
    assert_eq!(counts["events"], 1);
    assert_eq!(counts["audit_log"], 1);
    assert_eq!(counts["inbound_messages"], 3);
    let links: Vec<(i32, i32)> = sqlx::query_as(
        "SELECT child.revision,parent.revision FROM inbound_messages child
         JOIN inbound_messages parent ON parent.id=child.supersedes_id ORDER BY child.revision",
    )
    .fetch_all(&mut inspect)
    .await?;
    assert_eq!(links, vec![(2, 1), (3, 2)]);
    assert!(matches!(
        operation
            .restore_current_database(&mut Cursor::new(&fixture))
            .await,
        Err(DatabaseRestoreError::NotEmpty)
    ));
    println!(
        "Restored the complete snapshot only once; lifecycle, rollback and sequence checks followed"
    );
    Ok(())
}
