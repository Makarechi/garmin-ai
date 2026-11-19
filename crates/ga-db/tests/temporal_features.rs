use std::{
    env,
    error::Error,
    fs,
    io::{self, Write},
    str::FromStr,
};

use ga_db::{
    Database,
    health_analysis::{HealthAnalysisError, TemporalPurpose},
};
use ga_domain::timestamp::OffsetTimestamp;
use serde::Deserialize;
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

#[derive(Deserialize)]
struct Case {
    metrics: Vec<String>,
    events: Vec<String>,
    cutoff: String,
    purpose: String,
}

#[tokio::test]
#[ignore = "requires a disposable TimescaleDB containing the pinned temporal fixture"]
async fn select_feature_evidence() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected PostgreSQL fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Fixture database must end in _test")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    pool.close().await;
    let db = Database::connect_lazy(&url)
        .map_err(|_| io::Error::other("Expected disposable TimescaleDB fixture URL"))?;
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TEMPORAL_FEATURES")?)?)?;
    for case in cases {
        let metrics: Vec<_> = case.metrics.iter().map(String::as_str).collect();
        let events: Vec<OffsetDateTime> = case
            .events
            .iter()
            .map(|event| OffsetDateTime::parse(event, &Rfc3339))
            .collect::<Result<_, _>>()?;
        let purpose = match case.purpose.as_str() {
            "retrospective" => TemporalPurpose::Retrospective,
            "as_known" => TemporalPurpose::AsKnown,
            _ => return Err(io::Error::other("Unexpected test purpose").into()),
        };
        let mut output = io::stdout().lock();
        write!(output, "temporal: ")?;
        match db
            .temporal_features_with_offsets(
                &metrics,
                &events,
                OffsetDateTime::parse(&case.cutoff, &Rfc3339)?,
                purpose,
            )
            .await
        {
            Ok(features) => {
                let entries: Vec<_> = features
                    .into_iter()
                    .map(|(clock, metrics)| (OffsetTimestamp(clock).to_string(), metrics))
                    .collect();
                serde_json::to_writer(&mut output, &entries)?;
            }
            Err(
                err @ (HealthAnalysisError::InvalidTemporalTimestamp
                | HealthAnalysisError::InvalidKnowledgeCutoff
                | HealthAnalysisError::TemporalVersionLimit
                | HealthAnalysisError::TemporalCalendarOverflow),
            ) => serde_json::to_writer(
                &mut output,
                &json!({"error": err.to_string(),"class":if matches!(err,HealthAnalysisError::TemporalCalendarOverflow){"OverflowError"}else{"ValueError"}}),
            )?,
            Err(error) => return Err(error.into()),
        }
        writeln!(output)?;
    }
    if let Ok(status) = fs::read_to_string("/proc/self/status") {
        for line in status
            .lines()
            .filter(|line| line.starts_with("VmHWM:") || line.starts_with("VmRSS:"))
        {
            eprintln!("{line}");
        }
        for name in ["memory.peak", "memory.swap.peak", "memory.events"] {
            eprintln!(
                "{name}\n{}",
                fs::read_to_string(format!("/sys/fs/cgroup/{name}"))?
            );
        }
    }
    Ok(())
}

#[tokio::test]
async fn rejects_unsupported_temporal_precision_before_database_access()
-> Result<(), Box<dyn Error>> {
    let db =
        Database::connect_lazy("postgresql+psycopg://garmin@127.0.0.1:1/invalid_timestamp_test")
            .map_err(|_| "Invalid isolated fixture URL")?;
    let precise = OffsetDateTime::parse("2026-11-01T12:00:00.123456789Z", &Rfc3339)?;
    let supported = OffsetDateTime::parse("2026-11-01T12:00:00.123456Z", &Rfc3339)?;
    for (events, cutoff, metrics) in [
        (vec![precise], supported, vec!["unknown"]),
        (vec![precise], supported, vec![]),
        (vec![supported], precise, vec!["unknown"]),
        (vec![], precise, vec![]),
    ] {
        assert!(matches!(
            db.temporal_features_with_offsets(
                &metrics,
                &events,
                cutoff,
                TemporalPurpose::Retrospective
            )
            .await,
            Err(HealthAnalysisError::InvalidTemporalTimestamp)
        ));
    }
    Ok(())
}
