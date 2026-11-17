use std::{env, error::Error, fs};

use ga_domain::channel::{OutboundError, OutboundIntent};
use serde_json::Value;

#[test]
#[ignore = "compares stored intent documents against synthetic pinned Python fixtures"]
fn normalizes_outbound_content_and_rejects_foreign_references() -> Result<(), Box<dyn Error>> {
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_OUTBOUND_INTENTS")?)?)?;
    for mut case in cases {
        let input = case["input"].take();
        let name = case["name"].as_str().ok_or("Missing synthetic case name")?;
        let actual = OutboundIntent::normalize(input);
        if case["expected"].is_null() {
            assert!(actual.is_err(), "{name}");
            if matches!(name, "foreign_reply" | "foreign_replaces") {
                assert!(matches!(actual, Err(OutboundError::Reference)));
            }
            if name == "empty" {
                assert!(matches!(actual, Err(OutboundError::Empty)));
            }
        } else {
            let intent = actual.map_err(|error| format!("{name}: {error}"))?;
            assert!(intent.document() == &case["expected"], "{name}");
            assert_eq!(intent.channel().channel(), "fixture", "{name}");
            assert_eq!(intent.channel().instance_id(), "primary", "{name}");
        }
    }
    Ok(())
}
