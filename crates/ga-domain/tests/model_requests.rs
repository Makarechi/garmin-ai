use std::{env, error::Error, fs};

use ga_domain::{
    model_request::{ApprovedModelSchema, ModelRequestError, StructuredModelRequest},
    model_response::{ModelResponseError, StructuredProvider},
};
use serde_json::{Value, json};

#[test]
#[ignore = "requires pinned mock transport request bodies"]
fn prepares_only_application_provider_contracts() -> Result<(), Box<dyn Error>> {
    let expected: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_MODEL_REQUESTS")?)?)?;
    for (key, provider, schema_path) in [
        ("openai", StructuredProvider::OpenAi, "/text/format/schema"),
        (
            "anthropic",
            StructuredProvider::Anthropic,
            "/output_config/format/schema",
        ),
    ] {
        for (name, contract) in [
            ("agent-step", ApprovedModelSchema::AgentStep),
            ("interpretation", ApprovedModelSchema::Interpretation),
            ("safety-screen", ApprovedModelSchema::SafetyScreen),
            ("tracker-extraction", ApprovedModelSchema::TrackerExtraction),
            ("model-probe", ApprovedModelSchema::ModelProbe),
        ] {
            let request = StructuredModelRequest::prepare(
                provider,
                contract,
                "synthetic:model-v2",
                8192,
                "Synthetic instruction",
                "synthetic tracker label ☕",
            )?;
            let fixture = &expected[key][name];
            let wire: Value = serde_json::from_slice(&serde_json::to_vec(request.body())?)?;
            assert_eq!(wire, fixture["body"], "{key} {name}");
            let schema = wire.pointer(schema_path).ok_or("Missing wire schema")?;
            for entry in fixture["ordered_maps"]
                .as_array()
                .ok_or("Missing wire map order")?
            {
                let pointer = entry[0].as_str().ok_or("Invalid wire map pointer")?;
                let fields = schema
                    .pointer(pointer)
                    .and_then(Value::as_object)
                    .ok_or("Missing wire schema map")?;
                let actual = json!(fields.keys().collect::<Vec<_>>());
                assert_eq!(actual, entry[1], "{key} {name} {pointer}");
            }
        }
    }

    for (model, tokens) in [("", 256), ("model", 255), ("model", 8193)] {
        assert!(matches!(
            StructuredModelRequest::prepare(
                StructuredProvider::OpenAi,
                ApprovedModelSchema::SafetyScreen,
                model,
                tokens,
                "",
                "",
            ),
            Err(ModelRequestError::InvalidConfiguration)
        ));
    }
    assert!(matches!(
        StructuredModelRequest::prepare(
            StructuredProvider::Anthropic,
            ApprovedModelSchema::ModelProbe,
            &"Ж".repeat(201),
            256,
            "Synthetic instruction",
            "synthetic payload",
        ),
        Err(ModelRequestError::InvalidConfiguration)
    ));

    let probe = StructuredModelRequest::prepare(
        StructuredProvider::OpenAi,
        ApprovedModelSchema::ModelProbe,
        "synthetic",
        256,
        "Synthetic instruction",
        "synthetic payload",
    )?;
    let response = |result: Value| {
        json!({
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": result.to_string()},
            ]}],
        })
        .to_string()
        .into_bytes()
    };
    assert_eq!(
        probe
            .decode_response(&response(json!({"urgent":true})))?
            .value,
        json!({"urgent":true}),
    );
    assert!(matches!(
        probe.decode_response(&response(json!({"tracker field":"probe"}))),
        Err(ModelResponseError::InvalidOutput)
    ));
    Ok(())
}
