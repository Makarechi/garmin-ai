use std::{env, error::Error, fs, io, str::FromStr, sync::Arc};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_documents::{ApplyHealthPayload, DocumentStatus},
    raw_capture::LegacyCapture,
};
use ga_domain::{garmin_health::HealthReplacement, timestamp::StoredInstant};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

#[derive(Deserialize)]
struct Cases {
    at: String,
    steps: Vec<(String, String, i64, Value, Value, String)>,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and the pinned Python attested replacement fixture"]
async fn replaces_only_the_attested_interval_and_rolls_back_late_failures()
-> Result<(), Box<dyn Error>> {
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
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let root = tempfile::tempdir()?;
    let archive = Arc::new(Archive::open(&root.path().canonicalize()?.join("archive"))?);
    let cases: Cases =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_AUTHORITATIVE_REPLACEMENT")?)?)?;
    let at: StoredInstant = cases.at.parse()?;
    let fingerprint = "a".repeat(64);
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES
            ('account:garmin',jsonb_build_object('fingerprint',$1::text))",
    )
    .bind(&fingerprint)
    .execute(&inspect)
    .await?;
    let insight =
        || sqlx::query_scalar::<_, String>("SELECT status FROM insights").fetch_one(&inspect);

    for (name, day, seconds, document, proof, expected) in cases.steps {
        if name == "same_projection" {
            sqlx::query(
                "INSERT INTO insights(id,category,statement,evidence,sample_size,status,dedup_key)
                 VALUES (gen_random_uuid(),'synthetic','synthetic previous result','{}',1,
                         'candidate','synthetic-attestation')",
            )
            .execute(&inspect)
            .await?;
        }
        if name == "late_failure" {
            sqlx::query(
                "ALTER TABLE measurements ADD CONSTRAINT synthetic_attest CHECK (value!=88)",
            )
            .execute(&inspect)
            .await?;
        }
        let clock =
            StoredInstant::from_unix_microseconds(at.unix_microseconds() + seconds * 1_000_000)?;
        let capture = db
            .capture_legacy_json(
                LegacyCapture {
                    authenticated_fingerprint: &fingerprint,
                    endpoint: "heart_rate",
                    source_key: &day,
                    fetched_at: clock,
                    body: serde_json::to_vec(&document)?,
                },
                archive.clone(),
                PutLimits {
                    max_bytes: 1_048_576,
                    disk_reserve_bytes: 0,
                },
            )
            .await?;
        let request = ApplyHealthPayload {
            account_fingerprint: &fingerprint,
            endpoint: "heart_rate",
            calendar_key: &day,
            source_id: capture.id,
            requested_at: clock,
            timezone: "Europe/Bratislava",
        };
        let proof = HealthReplacement::restore("heart_rate", &proof)?;
        let result = match proof.as_ref() {
            Some(proof) => db.apply_attested_health_payload(request, proof).await?,
            None => db.apply_health_payload(request).await?,
        };
        let expect = match expected.as_str() {
            "normalized" => DocumentStatus::Normalized,
            "empty" => DocumentStatus::Empty,
            "unchanged" => DocumentStatus::Unchanged,
            "stale" => DocumentStatus::Stale,
            "error" => DocumentStatus::Error,
            _ => return Err("Invalid synthetic outcome".into()),
        };
        assert_eq!(result, expect, "{name}");
        if name == "same_projection" {
            assert_eq!(insight().await?, "candidate");
        }
        if name == "late_failure" {
            assert_eq!(insight().await?, "candidate");
            let values: Vec<f64> =
                sqlx::query_scalar("SELECT value FROM measurements ORDER BY value")
                    .fetch_all(&inspect)
                    .await?;
            assert_eq!(values, [72.0, 76.0, 87.0]);
            assert!(
                !sqlx::query_scalar::<_, bool>(
                    "SELECT EXISTS (SELECT 1 FROM measurement_revisions WHERE value=88)",
                )
                .fetch_one(&inspect)
                .await?
            );
            sqlx::query("ALTER TABLE measurements DROP CONSTRAINT synthetic_attest")
                .execute(&inspect)
                .await?;
        }
        if name == "empty_replacement" {
            assert_eq!(insight().await?, "superseded");
        }
    }
    Ok(())
}
