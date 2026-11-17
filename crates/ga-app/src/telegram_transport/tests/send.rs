use super::{bind, request, respond};
use crate::{
    telegram_input::TelegramBinding, telegram_reply::TelegramReplyError,
    telegram_transport::TelegramSendOutcome,
};

use std::{env, error::Error, fs, time::Duration};

use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::{Value, json};
use tokio::time::timeout;
use uuid::Uuid;

const CHAT: i64 = 1234567890123;
const OWNER: Uuid = Uuid::from_u128(1001);
const BINDING: TelegramBinding<'static> = TelegramBinding {
    owner_id: OWNER,
    instance_id: "primary",
    external_owner_id: CHAT,
};

fn intent() -> Result<OutboundIntent, Box<dyn Error>> {
    Ok(OutboundIntent::normalize(json!({
        "owner_id":OWNER.to_string(),
        "conversation_id":Uuid::from_u128(1002).to_string(),
        "channel_instance":{"channel":"telegram","instance_id":"primary"},
        "blocks":[{"text":"**literal** 1"},{"text":"second 🧪"},{"text":"third"}],
        "actions":[{"action_id":"confirm","label":"Confirm","operation_id":Uuid::from_u128(1003).to_string(),
                    "token":"synthetic_button_token_123"}],
    }))?)
}

#[tokio::test]
#[ignore = "compares the real send requests with the pinned Python Telegram SDK recording"]
async fn matches_neutral_send_message_calls() -> Result<(), Box<dyn Error>> {
    let expected: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_SEND")?)?)?;
    let (listener, transport) = bind().await?;
    let clock: StoredInstant = "2026-11-17T12:00:00Z".parse()?;
    let intent = intent()?;
    let server = async {
        let mut actual = Vec::new();
        for message in 701..=703 {
            let (mut socket, _) = listener.accept().await?;
            actual.push(request(&mut socket).await?);
            respond(&mut socket, "200 OK", &[], &accepted(message, CHAT)).await?;
        }
        Ok::<_, Box<dyn Error>>(actual)
    };
    let (requests, outcome) = timeout(Duration::from_secs(4), async {
        tokio::join!(
            server,
            transport.send_neutral_reply(BINDING, &intent, clock)
        )
    })
    .await?;
    assert_eq!(json!(requests?), expected);
    assert_eq!(
        outcome?,
        TelegramSendOutcome::Accepted {
            last_message_id: 703,
            parts: 3
        }
    );
    Ok(())
}

#[tokio::test]
async fn distinguishes_first_rejection_and_unknown_or_partial_sends() -> Result<(), Box<dyn Error>>
{
    let cases = [
        (
            0,
            "429 Too Many Requests",
            r#"{"ok":false,"parameters":{"retry_after":4}}"#.to_owned(),
            TelegramSendOutcome::RateLimited(Duration::from_secs(4)),
        ),
        (
            0,
            "403 Forbidden",
            "{}".to_owned(),
            TelegramSendOutcome::Rejected,
        ),
        (
            0,
            "200 OK",
            accepted(730, CHAT + 1),
            TelegramSendOutcome::Uncertain {
                last_message_id: None,
                accepted_parts: 0,
            },
        ),
        (
            0,
            "",
            String::new(),
            TelegramSendOutcome::Uncertain {
                last_message_id: None,
                accepted_parts: 0,
            },
        ),
        (
            1,
            "429 Too Many Requests",
            r#"{"ok":false,"parameters":{"retry_after":4}}"#.to_owned(),
            TelegramSendOutcome::Uncertain {
                last_message_id: Some(740),
                accepted_parts: 1,
            },
        ),
        (
            1,
            "400 Bad Request",
            "{}".to_owned(),
            TelegramSendOutcome::Uncertain {
                last_message_id: Some(740),
                accepted_parts: 1,
            },
        ),
        (
            1,
            "",
            String::new(),
            TelegramSendOutcome::Uncertain {
                last_message_id: Some(740),
                accepted_parts: 1,
            },
        ),
    ];
    let clock: StoredInstant = "2026-11-17T12:00:00Z".parse()?;
    let intent = intent()?;
    for (sent, status, body, outcome) in cases {
        let (listener, transport) = bind().await?;
        let server = async {
            for part in 0..=sent {
                let (mut socket, _) = listener.accept().await?;
                assert_eq!(request(&mut socket).await?["endpoint"], "sendMessage");
                if part < sent {
                    respond(&mut socket, "200 OK", &[], &accepted(740, CHAT)).await?;
                } else if !status.is_empty() {
                    respond(&mut socket, status, &[], &body).await?;
                }
            }
            assert!(
                timeout(Duration::from_millis(75), listener.accept())
                    .await
                    .is_err()
            );
            Ok::<_, Box<dyn Error>>(())
        };
        let (server, actual) = timeout(Duration::from_secs(4), async {
            tokio::join!(
                server,
                transport.send_neutral_reply(BINDING, &intent, clock)
            )
        })
        .await?;
        server?;
        assert_eq!(actual?, outcome);
    }
    let (listener, transport) = bind().await?;
    let wrong_bot = TelegramBinding {
        instance_id: "another-bot",
        ..BINDING
    };
    assert!(matches!(
        transport
            .send_neutral_reply(wrong_bot, &intent, clock)
            .await,
        Err(TelegramReplyError::WrongBinding)
    ));
    assert!(
        timeout(Duration::from_millis(75), listener.accept())
            .await
            .is_err()
    );
    Ok(())
}

fn accepted(message: i64, chat: i64) -> String {
    json!({"ok":true, "result":{"message_id":message,"chat":{"id":chat,"type":"private"}}})
        .to_string()
}

mod dispatch;
