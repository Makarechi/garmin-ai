use std::{env, error::Error, fs, future::ready};

use ga_domain::sha256_hex;
use serde_json::{Value, json};

use super::*;

#[tokio::test]
#[ignore = "compares pinned Python safety prompts and synthetic provider replies"]
async fn owner_text_fragments_match_the_original_safety_screen() -> Result<(), Box<dyn Error>> {
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_REPLY_SAFETY")?)?)?;
    let pattern = fixture["pattern"]
        .as_str()
        .ok_or("Missing synthetic text pattern")?;
    let cases = fixture["cases"].as_array().ok_or("Missing cases")?;
    for case in cases {
        let count = usize::try_from(case["length"].as_u64().ok_or("Missing text length")?)?;
        let text: String = pattern.chars().cycle().take(count).collect();
        let locale = case["locale"].as_str().ok_or("Missing locale")?;
        let mut replies = case["replies"]
            .as_array()
            .ok_or("Missing synthetic replies")?
            .iter();
        let mut requests = Vec::new();
        let result = screen_with(&text, locale, |instruction, prompt| {
            requests.push(json!([
                sha256_hex(instruction.as_bytes()),
                sha256_hex(prompt.as_bytes()),
                prompt.chars().count()
            ]));
            ready(
                replies
                    .next()
                    .filter(|reply| !reply.is_null())
                    .cloned()
                    .ok_or(SafetyScreenError::InvalidResponse),
            )
        })
        .await;
        let result = result.map(|decision| {
            json!({
                "intent": match decision.intent {
                    ReplySafetyIntent::Safety => "safety",
                    ReplySafetyIntent::Clarify => "clarify",
                },
                "language":decision.language,"oversized_notice":decision.oversized_notice,
            })
        });
        assert_eq!(json!(result.ok()), case["result"], "{}", case["name"]);
        assert_eq!(json!(requests), case["calls"], "{}", case["name"]);
    }
    println!(
        "{} safety decisions and exact prompt fragments matched",
        cases.len()
    );
    Ok(())
}
