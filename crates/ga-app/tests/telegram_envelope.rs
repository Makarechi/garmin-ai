use std::{env, error::Error, fs};

use ga_app::telegram_input::{CallbackResolution, TelegramBinding, TelegramInputError};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use uuid::Uuid;

#[test]
#[ignore = "compares with synthetic updates normalized by the pinned Python Telegram adapter"]
fn preserves_private_bindings_and_normalized_updates() -> Result<(), Box<dyn Error>> {
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_ENVELOPE")?)?)?;
    let owner = fixture["owner"]
        .as_i64()
        .ok_or("Missing external owner fixture")?;
    let person: Uuid = fixture["person"]
        .as_str()
        .ok_or("Missing owner fixture")?
        .parse()?;
    let received: StoredInstant = fixture["received"]
        .as_str()
        .ok_or("Missing fixture time")?
        .parse()?;
    let cases = fixture["cases"].as_array().ok_or("Missing cases")?;
    for case in cases {
        let name = case["name"].as_str().ok_or("Missing case name")?;
        let wire = &case["update"];
        let binding = TelegramBinding {
            external_owner_id: owner,
            owner_id: person,
            instance_id: case["instance"].as_str().ok_or("Missing instance")?,
        };
        let authenticated = binding.authenticate(wire);
        if case["error"] == "ForeignChat" {
            assert!(
                matches!(authenticated, Err(TelegramInputError::ForeignChat)),
                "{name}"
            );
            continue;
        }
        let update = authenticated?;
        assert_eq!(
            Some(update.update_id()),
            wire["update_id"].as_i64(),
            "{name}"
        );
        let time_known = case["known"].as_bool().ok_or("Missing time evidence")?;
        let mut resolved = case["action"].clone();
        let resolution = match case["resolution"].as_str() {
            Some("verified") => {
                assert_eq!(update.callback_data(), Some("synthetic callback data"));
                assert!(matches!(
                    update.normalize(received, CallbackResolution::Unavailable),
                    Err(TelegramInputError::ActionUnavailable)
                ));
                // A consumed transport token never becomes a new inbox capability.
                resolved["token"] = json!("synthetic-consumed-action");
                CallbackResolution::Verified {
                    action: &resolved,
                    time_known,
                }
            }
            Some("legacy") => CallbackResolution::Legacy { time_known },
            Some("unavailable") => CallbackResolution::Unavailable,
            _ => return Err("Unknown callback resolution fixture".into()),
        };
        let envelope = update.normalize(received, resolution)?;
        let mut actual = envelope.document();
        assert!(
            actual
                .as_object_mut()
                .and_then(|value| value.remove("message_id"))
                .is_some(),
            "{name}"
        );
        for attachment in actual["attachments"]
            .as_array_mut()
            .ok_or("Invalid attachments")?
        {
            assert!(
                attachment
                    .as_object_mut()
                    .and_then(|value| value.remove("attachment_id"))
                    .is_some(),
                "{name}"
            );
        }
        assert_eq!(actual, case["normalized"], "{name}");

        if name == "callback_known" {
            let without_time_proof = update.normalize(
                received,
                CallbackResolution::Verified {
                    action: &resolved,
                    time_known: false,
                },
            )?;
            assert!(without_time_proof.document()["occurred_at"].is_null());
            assert_eq!(without_time_proof.document()["time_precision"], "unknown");
        }
        if name == "text" {
            let mut ambiguous = wire.clone();
            ambiguous["edited_message"] = ambiguous["message"].clone();
            assert!(matches!(
                binding.authenticate(&ambiguous),
                Err(TelegramInputError::InvalidUpdate)
            ));
            ambiguous["edited_message"] = Value::Null;
            ambiguous["update_id"] = json!("510");
            assert!(matches!(
                binding.authenticate(&ambiguous),
                Err(TelegramInputError::InvalidUpdate)
            ));
        }
    }
    println!(
        "{} synthetic Telegram envelopes matched pinned Python",
        cases.len()
    );
    Ok(())
}
