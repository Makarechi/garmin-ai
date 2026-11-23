use std::{env, error::Error, fs};

use ga_app::telegram_format::message_parts;
use serde_json::{Value, json};

#[test]
fn splits_astral_boundary_and_keeps_code_out_of_styles() {
    let source = format!("**{}🧪 `code` tail**", "a".repeat(3499));
    let parts = message_parts(&source);
    assert_eq!(parts.len(), 2);
    assert_eq!(parts[0].text, "a".repeat(3499));
    assert_eq!(parts[0].entities[0].length, 3499);
    assert_eq!(parts[1].text, "🧪 code tail");
    assert_eq!(parts[1].entities.len(), 3);
    assert_eq!(
        parts[1].entities[0].as_json(),
        json!({"type":"code","offset":3,"length":4})
    );
    assert_eq!(
        parts[1].entities[1].as_json(),
        json!({"type":"bold","offset":0,"length":3})
    );
    assert_eq!(
        parts[1].entities[2].as_json(),
        json!({"type":"bold","offset":7,"length":5})
    );
}

#[test]
#[ignore = "compares synthetic Markdown with the pinned Telegram sender"]
fn matches_pinned_text_and_entities() -> Result<(), Box<dyn Error>> {
    let fixture: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_FORMAT")?)?)?;
    assert_ne!(fixture, [] as [serde_json::Value; 0]);
    for case in fixture {
        let source = case["source"].as_str().ok_or("Missing Markdown input")?;
        let actual: Vec<_> = message_parts(source)
            .iter()
            .map(|part| {
                let entities: Vec<_> = part
                    .entities
                    .iter()
                    .map(ga_app::telegram_format::TextEntity::as_json)
                    .collect();
                json!({"text": part.text, "entities": entities})
            })
            .collect();
        assert_eq!(json!(actual), case["parts"], "{}", case["name"]);
    }
    Ok(())
}
