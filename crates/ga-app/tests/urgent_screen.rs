use std::{env, error::Error, fs};

use ga_app::urgent_screen::{
    obvious_third_party_emergency, obvious_urgent_symptoms, urgent_notice,
};
use serde_json::Value;

#[test]
fn checks_recurrence_and_negation_without_a_provider() -> Result<(), Box<dyn Error>> {
    for (text, expected) in [
        ("I can't breathe", true),
        ("I don't have sudden severe pain", false),
        ("I have not only sudden severe pain", true),
        ("Внезапной сильной боли не было", false),
        ("У мамы инсульт", true),
        ("My husband had a stroke in 2010", false),
        ("У меня начались судороги два года назад", false),
        ("У меня судороги", true),
    ] {
        assert_eq!(obvious_urgent_symptoms(text)?, expected, "{text}");
    }
    Ok(())
}

#[test]
#[ignore = "compares the pinned Python safety decisions and messages"]
fn matches_pinned_local_safety_screen() -> Result<(), Box<dyn Error>> {
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_URGENT_SCREEN")?)?)?;
    let texts = fixture["texts"].as_array().ok_or("Missing fixture cases")?;
    assert!(texts.len() > 150);
    for case in texts {
        let input = case["text"].as_str().ok_or("Missing source text")?;
        assert_eq!(
            Some(obvious_urgent_symptoms(input)?),
            case["urgent"].as_bool(),
            "{input}"
        );
        assert_eq!(
            Some(obvious_third_party_emergency(input)?),
            case["third_party"].as_bool(),
            "{input}",
        );
    }
    for (locale, message) in fixture["messages"]
        .as_object()
        .ok_or("Missing locale map")?
    {
        assert_eq!(Some(urgent_notice(locale)), message.as_str(), "{locale}");
    }
    Ok(())
}
