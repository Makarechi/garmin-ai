use std::{env, error::Error, fs, path::PathBuf};

use ga_domain::{model_response::StructuredProvider, model_schema::compile_model_schema};
use serde_json::Value;

#[test]
#[ignore = "requires the pinned provider-schema comparison fixture"]
fn compares_native_wire_schemas() -> Result<(), Box<dyn Error>> {
    let root = PathBuf::from(env::var("GA_TEST_MODEL_SCHEMAS")?);
    let manifest: Value = serde_json::from_slice(&fs::read(root.join("manifest.json"))?)?;
    for (index, case) in manifest
        .as_array()
        .ok_or("Expected schema cases")?
        .iter()
        .enumerate()
    {
        let original = fs::read(root.join(format!("schema-{index}.json")))?;
        for (name, provider) in [
            ("openai", StructuredProvider::OpenAi),
            ("anthropic", StructuredProvider::Anthropic),
        ] {
            match compile_model_schema(provider, &original) {
                Ok(projected) => {
                    assert!(
                        case[name]["error"].is_null(),
                        "case {} {name}",
                        case["name"]
                    );
                    assert_eq!(
                        projected, case[name]["schema"],
                        "case {} {name}",
                        case["name"]
                    );
                }
                Err(error) => {
                    assert!(
                        case[name]["error"].is_string(),
                        "case {} {name}: {error}",
                        case["name"]
                    );
                }
            }
        }
    }
    Ok(())
}
