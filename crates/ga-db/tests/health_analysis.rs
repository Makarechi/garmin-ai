use std::{env, error::Error, fs};

use ga_db::{
    Database,
    health_analysis::{HealthAnalysisError, HealthPeriod},
};
use serde_json::Value;
use time::{Date, Month};

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB with synthetic Python daily observations"]
async fn describes_and_compares_the_pinned_daily_health_cases() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    if !url.ends_with("_test") {
        return Err("Use a disposable database ending in _test".into());
    }
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let fixtures: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_ANALYSIS_CASES")?)?)?;
    for case in &fixtures {
        let metric = case["metric"].as_str().ok_or("Missing fixture metric")?;
        let first = period(&case["first"])?;
        let actual = if case["second"].is_null() {
            db.personal_health_baseline(metric, first).await?
        } else {
            db.compare_health_periods(metric, first, period(&case["second"])?)
                .await?
        };
        compare(&case["expected"], &actual, metric);
    }
    let first = period(&fixtures.first().ok_or("Empty fixture")?["first"])?;
    assert!(matches!(
        db.personal_health_baseline("steps; DROP TABLE health_days", first)
            .await,
        Err(HealthAnalysisError::UnknownMetric)
    ));
    assert!(matches!(
        db.compare_health_periods("steps", first, first).await,
        Err(HealthAnalysisError::Overlap)
    ));
    assert!(
        HealthPeriod::new(
            Date::from_calendar_date(1, Month::March, 1)?,
            Date::from_calendar_date(1, Month::March, 2)?
        )
        .is_err()
    );
    assert!(
        HealthPeriod::new(
            Date::from_calendar_date(9999, Month::November, 1)?,
            Date::from_calendar_date(9999, Month::November, 2)?
        )
        .is_err()
    );
    Ok(())
}

fn period(fixture: &Value) -> Result<HealthPeriod, Box<dyn Error>> {
    Ok(HealthPeriod::new(date(&fixture[0])?, date(&fixture[1])?)?)
}

fn date(fixture: &Value) -> Result<Date, Box<dyn Error>> {
    let year = fixture[0].as_i64().ok_or("Missing year")?;
    let month = fixture[1].as_u64().ok_or("Missing month")?;
    let day = fixture[2].as_u64().ok_or("Missing day")?;
    Ok(Date::from_calendar_date(
        year.try_into()?,
        Month::try_from(u8::try_from(month)?)?,
        day.try_into()?,
    )?)
}

fn compare(expected: &Value, actual: &Value, context: &str) {
    match (expected, actual) {
        (Value::Number(expected), Value::Number(actual)) => {
            if let (Some(expected), Some(actual)) = (expected.as_f64(), actual.as_f64()) {
                let tolerance = 1e-12 * expected.abs().max(1.0);
                assert!(
                    (expected - actual).abs() <= tolerance,
                    "{context}: {expected:?} versus {actual:?}"
                );
            } else {
                assert_eq!(expected, actual, "{context}");
            }
        }
        (Value::Object(expected), Value::Object(actual)) => {
            assert_eq!(
                expected.keys().collect::<Vec<_>>(),
                actual.keys().collect::<Vec<_>>(),
                "{context}"
            );
            for (key, value) in expected {
                compare(value, &actual[key], &format!("{context}.{key}"));
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
