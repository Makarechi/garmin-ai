use std::{
    env,
    error::Error,
    io::{self, Read},
    str::FromStr,
    sync::Arc,
};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::{ApplyHealthPayload, DocumentStatus},
    raw_capture::LegacyCapture,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

const FIXTURES: &str = include_str!("../../../migration/fixtures/garmin_health_publication.json");
const DAY: &str = "2026-10-25";

#[tokio::test]
#[ignore = "uses a freshly migrated isolated TimescaleDB and synthetic health effects"]
async fn publishes_health_data_and_retains_history_atomically() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let opts =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = opts
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(opts.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, expected);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let account = "a".repeat(64);
    sqlx::query(
        "INSERT INTO app_state (key, value) VALUES ('account:garmin', jsonb_build_object('fingerprint',$1::text))
         ON CONFLICT (key) DO UPDATE SET value=excluded.value",
    )
    .bind(&account).execute(&inspect).await?;
    let root = tempfile::tempdir()?;
    let archive = Arc::new(Archive::open(&root.path().canonicalize()?.join("raw"))?);
    for step in serde_json::from_str::<Vec<Value>>(FIXTURES)? {
        let endpoint = step["endpoint"]
            .as_str()
            .ok_or("Missing synthetic endpoint")?;
        let second = step["second"].as_i64().ok_or("Missing synthetic clock")?;
        let fetched =
            StoredInstant::from_unix_microseconds(1_790_000_000_000_000 + second * 1_000_000)?;
        if step["force_failure"] == true {
            sqlx::query("ALTER TABLE measurements ADD CONSTRAINT synthetic_health_error CHECK (value != 88)")
                .execute(&inspect).await?;
        }
        let receipt = db
            .capture_legacy_json(
                LegacyCapture {
                    authenticated_fingerprint: &account,
                    endpoint,
                    source_key: DAY,
                    fetched_at: fetched,
                    body: serde_json::to_vec(&step["payload"])?,
                },
                Arc::clone(&archive),
                PutLimits {
                    max_bytes: 100_000,
                    disk_reserve_bytes: 0,
                },
            )
            .await?;
        let outcome = db
            .apply_health_payload(ApplyHealthPayload {
                account_fingerprint: &account,
                endpoint,
                calendar_key: DAY,
                source_id: receipt.id,
                requested_at: fetched,
                timezone: "Europe/Bratislava",
            })
            .await?;
        let expected = match step["status"].as_str() {
            Some("normalized") => DocumentStatus::Normalized,
            Some("unchanged") => DocumentStatus::Unchanged,
            Some("empty") => DocumentStatus::Empty,
            Some("stale") => DocumentStatus::Stale,
            Some("error") => DocumentStatus::Error,
            _ => return Err("Invalid synthetic outcome".into()),
        };
        assert_eq!(outcome, expected, "{endpoint} at {second}");
        if step["force_failure"] == true {
            let (resting, retained): (f64, i64) = sqlx::query_as(
                "SELECT (SELECT resting_hr FROM health_days WHERE day = DATE '2026-10-25'),
                    (SELECT count(*) FROM measurement_revisions WHERE value = 88)",
            )
            .fetch_one(&inspect)
            .await?;
            assert_eq!((resting, retained), (59.0, 0));
            let mut archived = Vec::new();
            archive
                .open_reader(&receipt.archive_key)?
                .read_to_end(&mut archived)?;
            assert_eq!(
                ga_domain::sha256_hex(&archived),
                receipt.archive_key.digest_hex()
            );
            sqlx::query("ALTER TABLE measurements DROP CONSTRAINT synthetic_health_error")
                .execute(&inspect)
                .await?;
        }
    }
    Ok(())
}
