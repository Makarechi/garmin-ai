use std::{env, error::Error, fs};

use ga_domain::{
    model_response::{GeminiResponseSchema, ModelResponseError},
    model_schema::{ModelSchemaError, compile_gemini_schema},
};
use serde_json::{Value, json};

#[test]
#[ignore = "requires the pinned Gemini SDK and application comparison fixture"]
fn compares_gemini_schema_and_response_contracts() -> Result<(), Box<dyn Error>> {
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_GEMINI_CONTRACTS")?)?)?;
    for case in fixture["schemas"]
        .as_array()
        .ok_or("Missing schema cases")?
    {
        let name = &case["name"];
        let projected = compile_gemini_schema(&serde_json::to_vec(&case["original"])?);
        if case["error"] == true {
            assert!(projected.is_err(), "{name}");
            continue;
        }
        let wire: Value = serde_json::from_slice(&serde_json::to_vec(&projected?)?)?;
        assert_eq!(wire, case["wire"], "{name}");
        for entry in case["ordered_maps"]
            .as_array()
            .ok_or("Missing wire map order")?
        {
            let pointer = entry[0].as_str().ok_or("Invalid wire schema pointer")?;
            let fields = wire
                .pointer(pointer)
                .and_then(Value::as_object)
                .ok_or("Missing wire schema map")?;
            assert_eq!(
                json!(fields.keys().collect::<Vec<_>>()),
                entry[1],
                "{name} {pointer}"
            );
        }
    }
    for case in fixture["replies"]
        .as_array()
        .ok_or("Missing response cases")?
    {
        let name = &case["name"];
        let local = GeminiResponseSchema::new(&case["original"])?;
        let output = local.decode(&serde_json::to_vec(&case["wire"])?);
        match output {
            Ok(output) => {
                assert!(case["error"].is_null(), "{name}");
                assert_eq!(output.value, case["value"], "{name}");
                let usage = output
                    .usage
                    .map(|usage| json!([usage.input_tokens, usage.output_tokens]));
                assert_eq!(json!(usage), case["usage"], "{name}");
            }
            Err(error) => assert_eq!(json!(format!("{error:?}")), case["error"], "{name}"),
        }
    }
    Ok(())
}

#[test]
fn bounds_gemini_schema_and_response_without_exposing_provider_data() -> Result<(), Box<dyn Error>>
{
    let mut defs = serde_json::Map::new();
    defs.insert(
        "Leaf".into(),
        json!({"type":"string", "description":"x".repeat(512)}),
    );
    let mut previous = "Leaf".to_owned();
    for i in 0..20 {
        let name = format!("Branch{i}");
        let reference = format!("#/$defs/{previous}");
        defs.insert(
            name.clone(),
            json!({"anyOf":[{"$ref":reference},{"$ref":reference}]}),
        );
        previous = name;
    }
    let expandable = json!({
        "type":"object", "properties":{"tree":{"$ref":format!("#/$defs/{previous}")}}, "$defs":defs,
    });
    assert_eq!(
        compile_gemini_schema(&serde_json::to_vec(&expandable)?),
        Err(ModelSchemaError::InvalidDocument)
    );
    assert_eq!(
        compile_gemini_schema(br#"{"type":"array","items":{"type":"string"}}"#),
        Err(ModelSchemaError::InvalidRoot)
    );
    let local = GeminiResponseSchema::new(&json!({"type":"object"}))?;
    assert!(matches!(
        local.decode(&vec![b' '; 1024 * 1024 + 1]),
        Err(ModelResponseError::TooLarge)
    ));
    assert!(matches!(
        local.decode(b"[]"),
        Err(ModelResponseError::InvalidEnvelope)
    ));
    assert!(matches!(
        local.decode(br#"{"steps":[]}"#),
        Err(ModelResponseError::InvalidOutput)
    ));
    Ok(())
}
