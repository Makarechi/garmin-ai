use std::{collections::BTreeSet, env, error::Error, fs};

use ga_domain::{
    sha256_hex,
    source_contract::{SourceCapabilities, SourcePage, SourceRequest},
};
use serde_json::{Value, json};

#[test]
#[ignore = "requires the synthetic source contract decisions from pinned Python"]
fn validates_pinned_source_pages_without_changing_record_digests() -> Result<(), Box<dyn Error>> {
    let fixture: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_SOURCE_CONTRACTS")?)?)?;
    for example in fixture {
        let name = example["name"].as_str().ok_or("Fixture has no case name")?;
        let wire = example["wire"].as_str().ok_or("Fixture has no input")?;
        let expected = &example["expected"];
        let parsed = SourcePage::from_json(wire.as_bytes(), 1000);
        assert_eq!(
            parsed.is_ok(),
            expected["valid"] == true,
            "{name}: {parsed:?}"
        );
        let Ok(page) = parsed else { continue };
        let encoded = page
            .records
            .iter()
            .map(|record| {
                String::from_utf8(record.canonical_json()?).map_err(Box::<dyn Error>::from)
            })
            .collect::<Result<Vec<_>, _>>()?;
        assert_eq!(json!(encoded), expected["records"], "{name}");
        let request = &example["request"];
        if request.is_null() {
            continue;
        }
        let previous: BTreeSet<_> = request["previous"]
            .as_array()
            .ok_or("Missing cursor history")?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .map(|value| sha256_hex(value.as_bytes()))
                    .ok_or("Invalid cursor history")
            })
            .collect::<Result<_, _>>()?;
        let outcome = SourceCapabilities::from_json(&example["caps"]).and_then(|capabilities| {
            page.check_request(&SourceRequest {
                instance_id: request["instance_id"]
                    .as_str()
                    .ok_or("Missing source ID")
                    .map_err(|_| ga_domain::source_contract::SourceContractError::Shape)?,
                start: parse_timestamp(&request["start"])?,
                end: parse_timestamp(&request["end"])?,
                cursor: request["cursor"].as_str(),
                previous_cursors: &previous,
                capabilities,
            })
        });
        assert_eq!(
            outcome.is_ok(),
            expected["request_valid"] == true,
            "{name}: {outcome:?}"
        );
        if let Ok(next) = outcome {
            assert_eq!(json!(next), expected["next"], "{name}");
        }
    }
    Ok(())
}

fn parse_timestamp(
    value: &Value,
) -> Result<ga_domain::timestamp::StoredInstant, ga_domain::source_contract::SourceContractError> {
    use ga_domain::source_contract::SourceContractError;
    value
        .as_str()
        .and_then(|text| text.parse().ok())
        .ok_or(SourceContractError::Timestamp)
}
