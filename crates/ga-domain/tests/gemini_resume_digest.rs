use std::{env, error::Error, fs};

use base64::{Engine, engine::general_purpose::STANDARD};
use ga_domain::model_request::{ApprovedModelSchema, GeminiModelRequest, ModelRequestError};
use serde_json::Value;

#[test]
#[ignore = "requires digests saved by the pinned Python provider gate"]
fn matches_persisted_python_gemini_fallback_identity() -> Result<(), Box<dyn Error>> {
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_GEMINI_RESUME_DIGEST")?)?)?;
    let text = fixture["text"].as_str().ok_or("Missing synthetic prompt")?;
    let audio = STANDARD.decode(fixture["audio"].as_str().ok_or("Missing synthetic audio")?)?;
    for case in fixture["cases"]
        .as_array()
        .ok_or("Missing fallback cases")?
    {
        let name = case["name"].as_str().ok_or("Missing contract case")?;
        let level = case["level"]
            .as_str()
            .ok_or("Missing thinking configuration")?;
        let models = read_strings(&case["models"])?;
        let categories = read_strings(&case["categories"])?;
        let schema = match name {
            "agent-step" => Some(ApprovedModelSchema::AgentStep),
            "interpretation" => Some(ApprovedModelSchema::Interpretation),
            "safety-screen" => Some(ApprovedModelSchema::SafetyScreen),
            "tracker-extraction" => Some(ApprovedModelSchema::TrackerExtraction),
            "model-probe" => Some(ApprovedModelSchema::ModelProbe),
            "transcript" => None,
            _ => return Err("Unknown synthetic contract".into()),
        };
        let prepared = |model: &str| match schema {
            Some(schema) => GeminiModelRequest::structured(
                schema,
                model,
                level,
                "Don't alter the original text",
                text,
            ),
            None => GeminiModelRequest::transcribe(model, level, &audio, "audio/ogg"),
        };
        let request = prepared(models[0])?;
        let digest = request.resume_fingerprint(&models, &categories)?;
        assert_eq!(digest, case["digest"], "{name} {level}");
        let retry = prepared(models[1])?;
        assert_eq!(retry.resume_fingerprint(&models, &categories)?, digest);
        let mut reversed = categories.clone();
        reversed.reverse();
        assert_eq!(request.resume_fingerprint(&models, &reversed)?, digest);
        assert_ne!(
            request.resume_fingerprint(&[models[1], models[0]], &categories)?,
            digest
        );
        assert!(matches!(
            request.resume_fingerprint(&models, &["imaginary"]),
            Err(ModelRequestError::InvalidConfiguration)
        ));
    }
    Ok(())
}

fn read_strings(value: &Value) -> Result<Vec<&str>, Box<dyn Error>> {
    Ok(value
        .as_array()
        .ok_or("Missing fixture list")?
        .iter()
        .map(|item| item.as_str().ok_or("Invalid fixture string"))
        .collect::<Result<_, _>>()?)
}
