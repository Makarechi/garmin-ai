use std::{
    env,
    error::Error,
    fs,
    io::{self, Write},
};

use ga_db::{
    Database,
    health_analysis::{HealthAnalysisError, TemporalPurpose},
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::json;

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
    let db = Database::connect_lazy(&env::var("GA_TEST_DATABASE_URL")?)
        .map_err(|_| io::Error::other("Expected disposable TimescaleDB fixture URL"))?;
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TEMPORAL_FEATURES")?)?)?;
    for case in cases {
        let metrics: Vec<_> = case.metrics.iter().map(String::as_str).collect();
        let events: Vec<StoredInstant> = case
            .events
            .iter()
            .map(|event| event.parse())
            .collect::<Result<_, _>>()?;
        let purpose = match case.purpose.as_str() {
            "retrospective" => TemporalPurpose::Retrospective,
            "as_known" => TemporalPurpose::AsKnown,
            _ => return Err(io::Error::other("Unexpected test purpose").into()),
        };
        let mut output = io::stdout().lock();
        write!(output, "temporal: ")?;
        match db
            .temporal_features(&metrics, &events, case.cutoff.parse()?, purpose)
            .await
        {
            Ok(features) => {
                let entries: Vec<_> = features
                    .into_iter()
                    .map(|(clock, metrics)| (clock.to_string(), metrics))
                    .collect();
                serde_json::to_writer(&mut output, &entries)?;
            }
            Err(
                err @ (HealthAnalysisError::InvalidKnowledgeCutoff
                | HealthAnalysisError::TemporalVersionLimit),
            ) => serde_json::to_writer(&mut output, &json!({"error": err.to_string()}))?,
            Err(error) => return Err(error.into()),
        }
        writeln!(output)?;
    }
    Ok(())
}
