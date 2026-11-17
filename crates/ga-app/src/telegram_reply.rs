//! Render a stored neutral reply for its verified Telegram private chat.

use std::{collections::HashSet, error::Error, fmt};

use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde_json::Value;

use crate::telegram_input::TelegramBinding;

const NEUTRAL_PART_LIMIT: usize = 3500;

pub struct TelegramReply {
    pub chat_id: i64,
    pub parts: Vec<String>,
    pub buttons: Vec<TelegramButton>,
}

pub struct TelegramButton {
    pub label: String,
    pub callback_data: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelegramReplyError {
    WrongBinding,
    Expired,
    UnsupportedAttachments,
    InvalidButton,
    InvalidContent,
}

impl fmt::Display for TelegramReplyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::WrongBinding => "Telegram intent targets a different private chat",
            Self::Expired => "Telegram intent or action has expired",
            Self::UnsupportedAttachments => "Telegram attachment delivery is unavailable",
            Self::InvalidButton => "Telegram intent requires valid signed action buttons",
            Self::InvalidContent => "Telegram intent has invalid content",
        })
    }
}
impl Error for TelegramReplyError {}

/// Prepare literal text and signed actions for the neutral Telegram channel.
pub fn prepare_reply(
    binding: TelegramBinding<'_>,
    intent: &OutboundIntent,
    now: StoredInstant,
) -> Result<TelegramReply, TelegramReplyError> {
    if !(1..(1_i64 << 52)).contains(&binding.external_owner_id)
        || intent.owner_id() != binding.owner_id
        || intent.channel().channel() != "telegram"
        || intent.channel().instance_id() != binding.instance_id
    {
        return Err(TelegramReplyError::WrongBinding);
    }
    let document = intent.document();
    let actions = document["actions"]
        .as_array()
        .ok_or(TelegramReplyError::InvalidContent)?;
    if expired(&document["expires_at"], now)
        || actions
            .iter()
            .any(|action| expired(&action["expires_at"], now))
    {
        return Err(TelegramReplyError::Expired);
    }
    if document["attachments"]
        .as_array()
        .is_some_and(|attachments| !attachments.is_empty())
    {
        return Err(TelegramReplyError::UnsupportedAttachments);
    }
    let mut reserved = HashSet::new();
    let mut buttons = Vec::new();
    for action in actions {
        let token = action["token"]
            .as_str()
            .filter(|token| (1..=64).contains(&token.len()) && reserved.insert(*token))
            .ok_or(TelegramReplyError::InvalidButton)?;
        let label = action["label"]
            .as_str()
            .ok_or(TelegramReplyError::InvalidButton)?;
        buttons.push(TelegramButton {
            label: label.to_owned(),
            callback_data: token.to_owned(),
        });
    }

    let mut parts = Vec::new();
    if !document["replaces"].is_null() {
        parts.push("Updated information:".to_owned());
    }
    if !document["reply_to"].is_null() {
        parts.push("Regarding the previous message:".to_owned());
    }
    let blocks = document["blocks"]
        .as_array()
        .ok_or(TelegramReplyError::InvalidContent)?;
    for block in blocks {
        let text = block["text"]
            .as_str()
            .ok_or(TelegramReplyError::InvalidContent)?;
        append_parts(&mut parts, text);
    }
    let form = &document["form"];
    if !form.is_null() {
        let key = form["definition_key"]
            .as_str()
            .ok_or(TelegramReplyError::InvalidContent)?;
        let version = form["definition_version"]
            .as_i64()
            .ok_or(TelegramReplyError::InvalidContent)?;
        append_parts(&mut parts, &format!("Form: {key} v{version}"));
    }
    if parts.is_empty() {
        parts.push("Выберите действие:".to_owned());
    }
    Ok(TelegramReply {
        chat_id: binding.external_owner_id,
        parts,
        buttons,
    })
}

fn expired(value: &Value, now: StoredInstant) -> bool {
    !value.is_null() && StoredInstant::from_legacy_input(value).is_none_or(|at| at <= now)
}

fn append_parts(parts: &mut Vec<String>, text: &str) {
    let mut start = 0;
    for (end, _) in text.char_indices().step_by(NEUTRAL_PART_LIMIT).skip(1) {
        parts.push(text[start..end].to_owned());
        start = end;
    }
    if start < text.len() {
        parts.push(text[start..].to_owned());
    }
}
