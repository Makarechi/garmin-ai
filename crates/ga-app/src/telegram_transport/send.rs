use std::time::Duration;

use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::{Value, json};

use super::{MAX_ACK_BYTES, TelegramTransport, TelegramTransportError};
use crate::{
    telegram_input::TelegramBinding,
    telegram_reply::{TelegramReplyError, prepare_reply},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelegramSendOutcome {
    Accepted {
        last_message_id: i64,
        parts: usize,
    },
    RateLimited(Duration),
    Rejected,
    Uncertain {
        last_message_id: Option<i64>,
        accepted_parts: usize,
    },
}

impl TelegramTransport {
    /// Attempt the parts of a reply once in order, keeping evidence of partial acceptance.
    /// The caller owns the durable delivery lease and the send-time permission fence.
    pub async fn send_neutral_reply(
        &self,
        binding: TelegramBinding<'_>,
        intent: &OutboundIntent,
        now: StoredInstant,
    ) -> Result<TelegramSendOutcome, TelegramReplyError> {
        let reply = prepare_reply(binding, intent, now)?;
        let buttons: Vec<_> = reply
            .buttons
            .iter()
            .map(|button| [json!({"text":button.label,"callback_data":button.callback_data})])
            .collect();
        let mut last_message_id = None;
        let parts = reply.parts.len();
        for (index, text) in reply.parts.iter().enumerate() {
            let mut request = json!({"chat_id":reply.chat_id,"text":text});
            if index == 0 && !buttons.is_empty() {
                request["reply_markup"] = json!({"inline_keyboard":buttons});
            }
            let result = self
                .call(&self.delivery, "sendMessage", &request, MAX_ACK_BYTES)
                .await
                .and_then(|message| observed_reference(&message, reply.chat_id));
            match result {
                Ok(message_id) => last_message_id = Some(message_id),
                Err(TelegramTransportError::RateLimited(delay)) if index == 0 => {
                    return Ok(TelegramSendOutcome::RateLimited(delay));
                }
                Err(TelegramTransportError::Authorization | TelegramTransportError::Rejected)
                    if index == 0 =>
                {
                    return Ok(TelegramSendOutcome::Rejected);
                }
                Err(_) => {
                    return Ok(TelegramSendOutcome::Uncertain {
                        last_message_id,
                        accepted_parts: index,
                    });
                }
            }
        }
        last_message_id
            .map(|last_message_id| TelegramSendOutcome::Accepted {
                last_message_id,
                parts,
            })
            .ok_or(TelegramReplyError::InvalidContent)
    }
}

fn observed_reference(message: &Value, chat_id: i64) -> Result<i64, TelegramTransportError> {
    if message["chat"]["id"].as_i64() != Some(chat_id)
        || message["chat"]["type"].as_str() != Some("private")
    {
        return Err(TelegramTransportError::InvalidResponse);
    }
    message["message_id"]
        .as_i64()
        .filter(|id| *id > 0)
        .ok_or(TelegramTransportError::InvalidResponse)
}
