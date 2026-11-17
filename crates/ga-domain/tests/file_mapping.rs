use std::{env, error::Error, fs};

use ga_domain::file_import::FileMapping;
use serde_json::{Value, json};
use uuid::Uuid;

#[test]
#[ignore = "requires the pinned Python fixture for file mappings"]
fn matches_pinned_mapping_decisions_and_confirmation() -> Result<(), Box<dyn Error>> {
    let cases: Vec<(Vec<u8>, Value)> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_FILE_MAPPING")?)?)?;
    let version = Uuid::parse_str("11111111-1111-4111-8111-111111111111")?;
    for (index, (bytes, expected)) in cases.into_iter().enumerate() {
        let actual = match FileMapping::from_json(&bytes) {
            Ok(mapping) => json!({
                "document": mapping.document(),
                "ordered_fields": mapping.field_columns().iter().map(|(field,_)| field).collect::<Vec<_>>(),
                "confirmation": mapping.confirmation_hash(&"f".repeat(64), &"e".repeat(64), version)?,
            }),
            Err(error) => json!({"error": error.to_string()}),
        };
        assert_eq!(actual, expected, "case {index}");
    }
    Ok(())
}
