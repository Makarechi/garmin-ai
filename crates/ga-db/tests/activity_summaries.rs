use std::{env, error::Error, io, str::FromStr, sync::Arc};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::{ActivitySummaryKind, ApplyActivitySummary, DocumentStatus},
    raw_capture::LegacyCapture,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

const ID: &str = "7000000002";
const FIXTURES: &str = include_str!("../../../migration/fixtures/garmin_activity_summaries.json");

#[tokio::test]
#[ignore = "uses a freshly migrated isolated TimescaleDB with a pinned Python comparison"]
async fn applies_partial_activity_summaries_and_retains_earlier_fields()
-> Result<(), Box<dyn Error>> {
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
    let existing: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM activities WHERE id = $1)")
            .bind(ID)
            .fetch_one(&inspect)
            .await?;
    assert!(!existing);

    let root = tempfile::tempdir()?;
    let archive = Arc::new(Archive::open(&root.path().canonicalize()?.join("raw"))?);
    let apply = async |kind: ActivitySummaryKind,
                       key: &str,
                       second: i64,
                       payload: Value|
           -> Result<DocumentStatus, Box<dyn Error>> {
        let fetched =
            StoredInstant::from_unix_microseconds(1_790_000_000_000_000 + second * 1_000_000)?;
        let raw = db
            .capture_legacy_json(
                LegacyCapture {
                    authenticated_fingerprint: &account,
                    endpoint: kind.endpoint(),
                    source_key: key,
                    fetched_at: fetched,
                    body: serde_json::to_vec(&payload)?,
                },
                Arc::clone(&archive),
                PutLimits {
                    max_bytes: 100_000,
                    disk_reserve_bytes: 0,
                },
            )
            .await?;
        Ok(db
            .apply_activity_summary(ApplyActivitySummary {
                account_fingerprint: &account,
                source_key: key,
                summary: kind,
                source_id: raw.id,
                requested_at: fetched,
                timezone: "UTC",
            })
            .await?)
    };
    for step in serde_json::from_str::<Vec<Value>>(FIXTURES)? {
        let mut document = step["summary"].clone();
        document["activityId"] = json!(ID);
        let second = step["second"].as_i64().ok_or("Missing fixture selection")?;
        let result = if let Some(page) = step["page"].as_str() {
            apply(ActivitySummaryKind::Page, page, second, json!([document])).await?
        } else {
            apply(ActivitySummaryKind::Single, ID, second, document).await?
        };
        assert_eq!(result, DocumentStatus::Normalized);
        if step["page"] == "page:0" {
            sqlx::query(
                "UPDATE app_state SET value = jsonb_build_object('requested_at', value->'requested_at')
                 WHERE key = $1",
            )
            .bind(format!("activity-version:{ID}"))
            .execute(&inspect)
            .await?;
        }
    }
    let applied: (String, String, f64, f64, f64, f64) = sqlx::query_as(
        "SELECT name, timezone, distance_m, avg_hr, cadence, extract(epoch FROM \"end\" - start)::float8 FROM activities WHERE id = $1",
    )
    .bind(ID)
    .fetch_one(&inspect)
    .await?;
    assert_eq!(
        applied,
        (
            "Synthetic run".to_owned(),
            "Europe/Bratislava".to_owned(),
            401.25,
            144.0,
            176.0,
            91.125
        )
    );
    assert_eq!(
        apply(
            ActivitySummaryKind::Page,
            "page:2",
            4,
            json!([
                {"activityId": "7000000010", "startTimeGMT": "2026-10-25 00:00:01", "duration": 10},
                {"activityId": "7000000011", "startTimeGMT": "invalid", "duration": 15}
            ]),
        )
        .await?,
        DocumentStatus::Error
    );
    let (was_written, original_status): (bool, String) = sqlx::query_as(
        "SELECT EXISTS (SELECT 1 FROM activities WHERE id = '7000000010'),
              (SELECT status FROM source_payloads WHERE source_key = 'page:2')",
    )
    .fetch_one(&inspect)
    .await?;
    assert_eq!((was_written, original_status.as_str()), (false, "error"));
    assert_eq!(
        apply(
            ActivitySummaryKind::Single, ID, 5,
            json!({"activityId": "7000000012", "startTimeGMT": "2026-10-25 00:00:01", "duration": 10}),
        ).await?,
        DocumentStatus::Error
    );
    Ok(())
}
