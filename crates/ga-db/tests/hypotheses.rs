use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    health_analysis::{HealthAnalysisError, HypothesisError},
};
use ga_domain::{hypothesis::HypothesisSpec, timestamp::StoredInstant};
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

async fn database() -> Result<(Database, PgPool), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected disposable fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Database must end in _test")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(target, actual);
    Ok((
        Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?,
        pool,
    ))
}

fn class(error: &HypothesisError) -> &'static str {
    match error {
        HypothesisError::Missing => "LookupError",
        HypothesisError::Conflict(_) => "Conflict",
        HypothesisError::Value(_) => "ValueError",
        HypothesisError::Spec => "ValidationError",
        HypothesisError::Calendar => "OverflowError",
        HypothesisError::Health(HealthAnalysisError::ReplayPending) => "ReplayUnavailable",
        _ => "RuntimeError",
    }
}

async fn setup(
    pool: &PgPool,
    case: &Value,
    identity: Uuid,
    score: Option<f64>,
) -> Result<(), Box<dyn Error>> {
    let mut txn = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *txn)
        .await?;
    sqlx::query("DELETE FROM app_state WHERE key LIKE 'hypothesis:%' OR key IN ('replay:generation','preferences:onboarding')").execute(&mut *txn).await?;
    if !case["before"].is_null() {
        sqlx::query("INSERT INTO app_state(key,value) VALUES ($1,$2::jsonb)")
            .bind(format!("hypothesis:{identity}"))
            .bind(serde_json::to_string(&case["before"])?)
            .execute(&mut *txn)
            .await?;
    }
    let fillers = case["fillers"].as_i64().unwrap_or(0);
    sqlx::query("INSERT INTO app_state(key,value) SELECT 'hypothesis:synthetic-filler:'||index,'{}'::jsonb FROM generate_series(0::bigint,$1::bigint-1) AS items(index)")
        .bind(fillers).execute(&mut *txn).await?;
    if case["has_generation"] == true {
        sqlx::query("INSERT INTO app_state(key,value) VALUES ('replay:generation',$1::jsonb)")
            .bind(serde_json::to_string(&case["generation"])?)
            .execute(&mut *txn)
            .await?;
    }
    sqlx::query("UPDATE health_days SET sleep_score=$1 WHERE day='2026-10-01'")
        .bind(case["health_score"].as_f64().or(score))
        .execute(&mut *txn)
        .await?;
    sqlx::query("DELETE FROM source_payloads WHERE id=$1")
        .bind(Uuid::from_u128(7_300_001))
        .execute(&mut *txn)
        .await?;
    if case["replay"] == true {
        sqlx::query("INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,fetched_at,parser_version,status,payload) VALUES ($1,'garmin_connect','daily','2026-09-10',repeat('c',64),'sha256:'||repeat('c',64),'2026-09-10T00:00:00Z',0,'pending','{\"totalSteps\":0}'::jsonb)")
            .bind(Uuid::from_u128(7_300_001)).execute(&mut *txn).await?;
    }
    txn.commit().await?;
    Ok(())
}

fn compare(expected: &Value, actual: &Value, context: &str) {
    match (expected, actual) {
        (Value::Number(expected), Value::Number(actual)) => {
            let expected = expected.as_f64().unwrap_or(f64::NAN);
            let actual = actual.as_f64().unwrap_or(f64::NAN);
            assert_eq!(
                expected.to_bits(),
                actual.to_bits(),
                "{context}: {expected} versus {actual}"
            );
        }
        (Value::Object(expected), Value::Object(actual)) => {
            assert_eq!(expected.len(), actual.len(), "{context}");
            for (key, expected) in expected {
                assert!(actual.contains_key(key), "{context}.{key} is missing");
                let actual = &actual[key];
                compare(expected, actual, &format!("{context}.{key}"));
            }
        }
        (Value::Array(expected), Value::Array(actual)) => {
            assert_eq!(expected.len(), actual.len(), "{context}");
            for (index, (expected, actual)) in expected.iter().zip(actual).enumerate() {
                compare(expected, actual, &format!("{context}[{index}]"));
            }
        }
        _ => assert_eq!(expected, actual, "{context}"),
    }
}

#[tokio::test]
#[ignore = "uses dedicated frozen hypothesis lifecycle fixtures"]
async fn compares_hypothesis_history() -> Result<(), Box<dyn Error>> {
    let (database, pool) = database().await?;
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_HYPOTHESES")?)?)?;
    let owner = Uuid::parse_str(fixture["owner"].as_str().ok_or("Missing fixture owner")?)?;
    let identity = Uuid::parse_str("12345678-1234-5678-9234-567812345678")?;
    let score: Option<f64> =
        sqlx::query_scalar("SELECT sleep_score FROM health_days WHERE day='2026-10-01'")
            .fetch_one(&pool)
            .await?;
    for case in fixture["cases"].as_array().ok_or("Missing fixture cases")? {
        setup(&pool, case, identity, score).await?;
        let now: StoredInstant = case["now"]
            .as_str()
            .ok_or("Missing fixture clock")?
            .parse()?;
        let configured = case["configured"]
            .as_str()
            .ok_or("Missing fixture timezone")?;
        let result = match case["action"].as_str() {
            Some("register") => {
                database
                    .register_hypothesis(
                        owner,
                        &HypothesisSpec::from_json(&case["spec"])?,
                        now,
                        configured,
                    )
                    .await
            }
            Some("recheck") => {
                database
                    .recheck_hypothesis(owner, identity, now, configured)
                    .await
            }
            Some("stop") => database.stop_hypothesis(owner, identity, now).await,
            Some("fetch") => database.hypothesis(identity).await,
            _ => return Err("Unknown fixture operation".into()),
        };
        let name = case["name"].as_str().ok_or("Missing fixture name")?;
        if let Some(expected) = case["error"].as_str() {
            let error = result
                .err()
                .ok_or_else(|| format!("Expected rejection {name}"))?;
            assert_eq!(class(&error), expected, "{name}");
            if matches!(
                error,
                HypothesisError::Conflict(_) | HypothesisError::Value(_) | HypothesisError::Missing
            ) {
                assert_eq!(
                    Some(error.to_string().as_str()),
                    case["message"].as_str(),
                    "{name}"
                );
            }
        } else {
            compare(&case["expected"], &result?, name);
        }
        let stored: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                .bind(format!("hypothesis:{identity}"))
                .fetch_optional(&pool)
                .await?;
        let stored = stored
            .map(|value| serde_json::from_str::<Value>(&value))
            .transpose()?
            .unwrap_or(Value::Null);
        compare(
            if case["error"].is_string() {
                &case["before"]
            } else {
                &case["stored"]
            },
            &stored,
            &format!("{name} stored state"),
        );
    }
    let mut concurrent = fixture["cases"][0].clone();
    concurrent["fillers"] = serde_json::json!(99);
    setup(&pool, &concurrent, identity, score).await?;
    let first_spec = HypothesisSpec::from_json(&concurrent["spec"])?;
    let mut second_document = concurrent["spec"].clone();
    second_document["id"] = serde_json::json!(Uuid::from_u128(7_400_101).to_string());
    let second_spec = HypothesisSpec::from_json(&second_document)?;
    let now: StoredInstant = concurrent["now"].as_str().ok_or("Missing clock")?.parse()?;
    let (first, second) = tokio::join!(
        database.register_hypothesis(owner, &first_spec, now, "UTC"),
        database.register_hypothesis(owner, &second_spec, now, "UTC"),
    );
    let outcomes = [first, second];
    assert_eq!(outcomes.iter().filter(|value| value.is_ok()).count(), 1);
    for error in outcomes.iter().filter_map(|value| value.as_ref().err()) {
        assert!(
            matches!(
                error,
                HypothesisError::Write(ga_db::diary::DiaryWriteError::Busy)
                    | HypothesisError::Value("Hypothesis storage limit reached")
            ),
            "Unexpected concurrent registration failure {error:?}"
        );
    }
    let mut reused = 0;
    for spec in [&first_spec, &second_spec] {
        match database.register_hypothesis(owner, spec, now, "UTC").await {
            Ok(value) => {
                assert_eq!(value["spec"], *spec.document());
                reused += 1;
            }
            Err(HypothesisError::Value("Hypothesis storage limit reached")) => (),
            Err(error) => return Err(format!("Unexpected retry failure {error:?}").into()),
        }
    }
    assert_eq!(reused, 1);
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM app_state WHERE key LIKE 'hypothesis:%'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(
        count, 100,
        "Concurrent registration must retain the storage bound"
    );
    pool.close().await;
    if env::var_os("GA_TEST_HYPOTHESIS_RESOURCE").is_some() {
        let status = fs::read_to_string("/proc/self/status")?;
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
