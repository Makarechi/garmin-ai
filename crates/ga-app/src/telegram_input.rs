use std::{error::Error, fmt};

use ga_domain::{channel::InboundEnvelope, timestamp::StoredInstant};
use serde_json::{Value, json};
use uuid::Uuid;

const TELEGRAM_NAMESPACE: Uuid = Uuid::from_u128(0x5ddd62fc_6890_44b6_86a2_20f77524378f);

#[derive(Clone, Copy)]
pub struct TelegramBinding<'a> {
    pub external_owner_id: i64,
    pub owner_id: Uuid,
    pub instance_id: &'a str,
}

pub enum CallbackResolution<'a> {
    Unavailable,
    Verified { action: &'a Value, time_known: bool },
    Legacy { time_known: bool },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelegramInputError {
    ForeignChat,
    InvalidUpdate,
    ActionUnavailable,
    InvalidEnvelope,
}

impl fmt::Display for TelegramInputError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::ForeignChat => "Telegram update is outside the configured private chat",
            Self::InvalidUpdate => "Telegram update is invalid",
            Self::ActionUnavailable => "Telegram action is unavailable or already used",
            Self::InvalidEnvelope => "Telegram update could not be normalized",
        })
    }
}
impl Error for TelegramInputError {}

enum InputKind<'wire> {
    Message,
    Edit,
    Callback(&'wire Value),
}

/// Telegram transport authentication happens before the owner and private-chat check here.
pub struct AuthenticatedTelegramUpdate<'wire, 'binding> {
    binding: TelegramBinding<'binding>,
    update_id: i64,
    message: &'wire Value,
    kind: InputKind<'wire>,
}

impl<'binding> TelegramBinding<'binding> {
    pub fn authenticate<'wire>(
        self,
        wire: &'wire Value,
    ) -> Result<AuthenticatedTelegramUpdate<'wire, 'binding>, TelegramInputError> {
        let (kind, message, sender) = match (
            wire.get("message").filter(|value| !value.is_null()),
            wire.get("edited_message").filter(|value| !value.is_null()),
            wire.get("callback_query").filter(|value| !value.is_null()),
        ) {
            (Some(message), None, None) => (InputKind::Message, message, &message["from"]),
            (None, Some(message), None) => (InputKind::Edit, message, &message["from"]),
            (None, None, Some(callback)) => (
                InputKind::Callback(callback),
                &callback["message"],
                &callback["from"],
            ),
            _ => return Err(TelegramInputError::InvalidUpdate),
        };
        let chat = &message["chat"];
        if !(1..(1_i64 << 52)).contains(&self.external_owner_id)
            || sender["id"].as_i64() != Some(self.external_owner_id)
            || chat["id"].as_i64() != Some(self.external_owner_id)
            || chat["type"].as_str() != Some("private")
        {
            return Err(TelegramInputError::ForeignChat);
        }
        let update_id = wire["update_id"]
            .as_i64()
            .filter(|id| *id > 0)
            .ok_or(TelegramInputError::InvalidUpdate)?;
        Ok(AuthenticatedTelegramUpdate {
            binding: self,
            update_id,
            message,
            kind,
        })
    }
}

impl AuthenticatedTelegramUpdate<'_, '_> {
    pub fn update_id(&self) -> i64 {
        self.update_id
    }

    pub fn callback_data(&self) -> Option<&str> {
        match self.kind {
            InputKind::Callback(callback) => callback["data"].as_str(),
            _ => None,
        }
    }

    /// Keep callback timing evidence separate from the provider-controlled JSON.
    pub fn normalize(
        &self,
        received_at: StoredInstant,
        action_resolution: CallbackResolution<'_>,
    ) -> Result<InboundEnvelope, TelegramInputError> {
        let instance = self.binding.instance_id;
        let owner_id = self.binding.owner_id;
        let external_owner = self.binding.external_owner_id;
        let conversation = Uuid::new_v5(
            &TELEGRAM_NAMESPACE,
            format!("{owner_id}:telegram:{instance}:{external_owner}").as_bytes(),
        );
        let channel = json!({"channel":"telegram", "instance_id":instance});
        let external_message = optional_id(&self.message["message_id"])?;
        let reply_to = optional_id(&self.message["reply_to_message"]["message_id"])?
            .map(|id| json!({"channel_instance":channel,"external_message_id":id}));
        let mut text = optional_text(&self.message["text"])?
            .filter(|text| !text.is_empty())
            .or(optional_text(&self.message["caption"])?);
        let mut action = None;
        let mut attachment = None;
        let (kind, revision, occurred_at) = match self.kind {
            InputKind::Callback(_) => {
                let (resolved, known_time) = self.resolve_action(action_resolution)?;
                action = Some(resolved);
                ("action", 1, known_time.then_some(received_at))
            }
            InputKind::Edit => {
                let revision = i32::try_from(self.update_id.max(2))
                    .map_err(|_| TelegramInputError::InvalidEnvelope)?;
                ("edit", revision, message_time(self.message)?)
            }
            InputKind::Message => {
                let kind = if let Some(voice) = voice(self.message)? {
                    text = Some(text.unwrap_or(""));
                    attachment = Some(voice);
                    "voice"
                } else if text.is_none() {
                    "system"
                } else {
                    "text"
                };
                (kind, 1, message_time(self.message)?)
            }
        };
        let envelope = json!({
            "owner_id":owner_id.to_string(),
            "channel_instance": channel,
            "conversation_id":conversation.to_string(),
            "external_event_id":self.update_id.to_string(),
            "external_message_id":external_message,
            "sender_ref":external_owner.to_string(),
            "occurred_at":occurred_at.map(|at| at.to_string()),
            "received_at":received_at.to_string(),
            "time_precision":if occurred_at.is_some() {"second"} else {"unknown"},
            "kind":kind,
            "text":text,
            "reply_to":reply_to,
            "attachments":attachment.into_iter().collect::<Vec<_>>(),
            "action":action,
            "revision":revision,
        });
        let bytes =
            serde_json::to_vec(&envelope).map_err(|_| TelegramInputError::InvalidEnvelope)?;
        InboundEnvelope::parse(&bytes).map_err(|_| TelegramInputError::InvalidEnvelope)
    }

    fn resolve_action(
        &self,
        resolution: CallbackResolution<'_>,
    ) -> Result<(Value, bool), TelegramInputError> {
        match resolution {
            CallbackResolution::Unavailable => Err(TelegramInputError::ActionUnavailable),
            CallbackResolution::Verified { action, time_known } => {
                let mut action = action
                    .as_object()
                    .cloned()
                    .ok_or(TelegramInputError::InvalidEnvelope)?;
                action.insert("token".into(), Value::Null);
                Ok((Value::Object(action), time_known))
            }
            CallbackResolution::Legacy { time_known } => {
                let data = self.callback_data().filter(|value| !value.is_empty());
                let operation = Uuid::new_v5(
                    &TELEGRAM_NAMESPACE,
                    format!("action:{}", self.update_id).as_bytes(),
                );
                Ok((
                    json!({
                        "action_id":data.unwrap_or("legacy"),
                        "label":data.unwrap_or("legacy action"),
                        "operation_id":operation.to_string(),
                    }),
                    time_known,
                ))
            }
        }
    }
}

fn optional_id(value: &Value) -> Result<Option<String>, TelegramInputError> {
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_i64()
        .filter(|value| *value >= 0)
        .map(|value| Some(value.to_string()))
        .ok_or(TelegramInputError::InvalidUpdate)
}

fn optional_text(value: &Value) -> Result<Option<&str>, TelegramInputError> {
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_str()
        .map(Some)
        .ok_or(TelegramInputError::InvalidUpdate)
}

fn voice(message: &Value) -> Result<Option<Value>, TelegramInputError> {
    let voice = &message["voice"];
    if voice.is_null() || voice.as_object().is_some_and(serde_json::Map::is_empty) {
        return Ok(None);
    }
    let identifier = voice["file_id"]
        .as_str()
        .ok_or(TelegramInputError::InvalidUpdate)?;
    let media_type = optional_text(&voice["mime_type"])?
        .filter(|value| !value.is_empty())
        .unwrap_or("audio/ogg");
    Ok(Some(json!({
        "kind":"voice", "media_type":media_type, "size_bytes":voice["file_size"],
        "external_id":identifier,
    })))
}

fn message_time(message: &Value) -> Result<Option<StoredInstant>, TelegramInputError> {
    let value = message.get("edit_date").unwrap_or(&message["date"]);
    if let Some(seconds) = value.as_i64() {
        return seconds
            .checked_mul(1_000_000)
            .and_then(|micros| StoredInstant::from_unix_microseconds(micros).ok())
            .map(Some)
            .ok_or(TelegramInputError::InvalidUpdate);
    }
    let Some(seconds) = value.as_f64() else {
        return Ok(None);
    };
    let micros = (seconds * 1_000_000.0).round_ties_even();
    if !(-9_223_372_036_854_775_808.0..9_223_372_036_854_775_808.0).contains(&micros) {
        return Err(TelegramInputError::InvalidUpdate);
    }
    StoredInstant::from_unix_microseconds(micros as i64)
        .map(Some)
        .map_err(|_| TelegramInputError::InvalidUpdate)
}
