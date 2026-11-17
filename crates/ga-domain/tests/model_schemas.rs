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
                    let wire = serde_json::to_vec(&projected)?;
                    // Parse the actual wire bytes with an ordered representation even if
                    // the compiler accidentally starts using sorted JSON maps.
                    let wire = jiter::JsonValue::parse(&wire, false)?;
                    for entry in case[name]["ordered_maps"]
                        .as_array()
                        .ok_or("Missing wire map order")?
                    {
                        let pointer = entry[0].as_str().ok_or("Invalid wire map pointer")?;
                        let jiter::JsonValue::Object(fields) =
                            lookup(&wire, pointer).ok_or("Missing wire schema map")?
                        else {
                            return Err("Expected wire schema map".into());
                        };
                        let expected: Vec<_> = entry[1]
                            .as_array()
                            .ok_or("Missing wire map keys")?
                            .iter()
                            .map(|key| key.as_str().ok_or("Invalid wire map key"))
                            .collect::<Result<_, _>>()?;
                        let actual: Vec<_> = fields.iter().map(|(key, _)| key.as_ref()).collect();
                        assert_eq!(actual, expected, "case {} {name} {pointer}", case["name"]);
                    }
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

fn lookup<'a, 'src>(
    mut node: &'a jiter::JsonValue<'src>,
    pointer: &str,
) -> Option<&'a jiter::JsonValue<'src>> {
    for segment in pointer.strip_prefix('/')?.split('/') {
        node = match node {
            jiter::JsonValue::Object(fields) => {
                let segment = segment.replace("~1", "/").replace("~0", "~");
                let (_, value) = fields.iter().find(|(name, _)| name.as_ref() == segment)?;
                value
            }
            jiter::JsonValue::Array(values) => values.get(segment.parse::<usize>().ok()?)?,
            _ => return None,
        };
    }
    Some(node)
}
