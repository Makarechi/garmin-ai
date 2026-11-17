use std::{env, error::Error, fs};

use ga_app::{
    telegram_input::TelegramBinding,
    telegram_reply::{TelegramReplyError, prepare_reply},
};
use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::{Value, json};
use uuid::Uuid;

#[test]
#[ignore = "compares synthetic reply rendering with the pinned Python Telegram adapter"]
fn matches_neutral_telegram_reply_rendering() -> Result<(), Box<dyn Error>> {
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_REPLY")?)?)?;
    let owner_id: Uuid = fixture["owner"].as_str().ok_or("Missing owner")?.parse()?;
    let now: StoredInstant = fixture["now"].as_str().ok_or("Missing time")?.parse()?;
    let binding = TelegramBinding {
        owner_id,
        instance_id: "primary",
        external_owner_id: fixture["chat"].as_i64().ok_or("Missing private chat")?,
    };
    let cases = fixture["cases"].as_array().ok_or("Missing cases")?;
    assert!(!cases.is_empty());
    for case in cases {
        let intent = OutboundIntent::normalize(case["intent"].clone())?;
        let result = prepare_reply(binding, &intent, now);
        let (state, calls) = match result {
            Err(error) => {
                let state = match error {
                    TelegramReplyError::WrongBinding | TelegramReplyError::InvalidButton => {
                        "failed"
                    }
                    TelegramReplyError::Expired => "expired",
                    TelegramReplyError::UnsupportedAttachments => "queued",
                    TelegramReplyError::InvalidContent => return Err(error.into()),
                };
                (state, Vec::<Value>::new())
            }
            Ok(reply) => {
                let buttons: Vec<_> = reply.buttons.iter().map(|button|
                    json!([{"text": button.label, "callback_data": button.callback_data}])
                ).collect();
                let calls: Vec<_> = reply
                    .parts
                    .iter()
                    .enumerate()
                    .map(|(index, part)| {
                        json!({
                            "chat": reply.chat_id, "text": part, "parse_mode": Value::Null,
                            "keyboard": if index == 0 { &buttons[..] } else { &[] },
                        })
                    })
                    .collect();
                ("provider_accepted", calls)
            }
        };
        assert_eq!(state, case["state"], "{}", case["name"]);
        assert_eq!(json!(calls), case["calls"], "{}", case["name"]);
    }
    Ok(())
}

#[test]
fn checks_the_owner_and_refuses_unsigned_or_duplicate_buttons() -> Result<(), Box<dyn Error>> {
    let owner = Uuid::from_u128(801);
    let channel = json!({"channel":"telegram","instance_id":"primary"});
    let action = json!({"action_id":"synthetic","label":"Confirm","operation_id":Uuid::from_u128(803).to_string()});
    let doc = json!({
        "owner_id":owner.to_string(), "conversation_id":Uuid::from_u128(802).to_string(),
        "channel_instance":channel, "actions":[action],
    });
    let intent = OutboundIntent::normalize(doc.clone())?;
    let now: StoredInstant = "2026-11-17T12:00:00Z".parse()?;
    let binding = TelegramBinding {
        owner_id: owner,
        instance_id: "primary",
        external_owner_id: 1234567890123,
    };
    let wrong_owner = TelegramBinding {
        owner_id: Uuid::from_u128(804),
        ..binding
    };
    assert!(matches!(
        prepare_reply(wrong_owner, &intent, now),
        Err(TelegramReplyError::WrongBinding)
    ));
    assert!(matches!(
        prepare_reply(binding, &intent, now),
        Err(TelegramReplyError::InvalidButton)
    ));
    let mut doc = doc;
    let mut action = action;
    action["token"] = json!("synthetic_duplicate_callback");
    doc["actions"] = json!([action, action]);
    let repeated = OutboundIntent::normalize(doc)?;
    assert!(matches!(
        prepare_reply(binding, &repeated, now),
        Err(TelegramReplyError::InvalidButton)
    ));
    Ok(())
}
