use std::{error::Error, fmt};

use serde_json::{Map, Value, json};
use uuid::Uuid;

use super::{
    ChannelInstance, InboundError, action, attachment, datetime, datetime_text, integer,
    message_ref, object, optional, string, uuid,
};

const FIELDS: &[&str] = &[
    "intent_id",
    "owner_id",
    "conversation_id",
    "channel_instance",
    "blocks",
    "evidence_refs",
    "form",
    "actions",
    "attachments",
    "expires_at",
    "privacy",
    "priority",
    "preferred_medium",
    "initiative",
    "policy_revision",
    "scheduled_day",
    "logical_notification_id",
    "reply_to",
    "replaces",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutboundError {
    Fields,
    Empty,
    Reference,
}

impl fmt::Display for OutboundError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Fields => "Outbound intent has invalid fields",
            Self::Empty => "Outbound intent requires content",
            Self::Reference => "Outbound message reference is outside the destination",
        })
    }
}

impl Error for OutboundError {}

impl From<InboundError> for OutboundError {
    fn from(_: InboundError) -> Self {
        Self::Fields
    }
}

/// Channel-neutral delivery content with its owner and recipient namespace bound.
pub struct OutboundIntent {
    intent_id: Uuid,
    owner_id: Uuid,
    conversation_id: Uuid,
    channel: ChannelInstance,
    document: Value,
}

impl OutboundIntent {
    /// Normalize an owned document so large text blocks can be retained without copies.
    pub fn normalize(mut document: Value) -> Result<Self, OutboundError> {
        let fields = object(&document, FIELDS)?;
        let intent_id = fields
            .get("intent_id")
            .map_or_else(|| Ok(Uuid::new_v4()), uuid)?;
        let owner_id = uuid(&document["owner_id"])?;
        let conversation_id = uuid(&document["conversation_id"])?;
        let channel = ChannelInstance::parse(&document["channel_instance"])?;
        let namespace = channel.document();
        let reply_to = optional(fields.get("reply_to"), message_ref)?;
        let replaces = optional(fields.get("replaces"), message_ref)?;
        if [&reply_to, &replaces]
            .into_iter()
            .flatten()
            .any(|reference| reference["channel_instance"] != namespace)
        {
            return Err(OutboundError::Reference);
        }

        let blocks = array(fields, "blocks", 100)?;
        for block in blocks {
            let block = object(block, &["text"])?;
            string(block.get("text"), 1, 100_000)?;
        }
        let evidence = array(fields, "evidence_refs", 100)?;
        if evidence
            .iter()
            .any(|reference| reference.as_str().is_none())
        {
            return Err(OutboundError::Fields);
        }
        let form = optional(fields.get("form"), form)?;
        let actions: Vec<_> = array(fields, "actions", 50)?
            .iter()
            .map(|value| action(value).map(|(_, document)| document))
            .collect::<Result<_, _>>()?;
        let attachments = array(fields, "attachments", 20)?
            .iter()
            .map(attachment)
            .collect::<Result<Vec<_>, _>>()?;
        if blocks.is_empty() && form.is_none() && actions.is_empty() && attachments.is_empty() {
            return Err(OutboundError::Empty);
        }

        let expires_at = optional(fields.get("expires_at"), datetime)?.map(datetime_text);
        let privacy = fields
            .get("privacy")
            .map_or(Ok("private"), |value| string(Some(value), 1, 50))?;
        let priority = fields
            .get("priority")
            .map_or(Some(0), integer)
            .filter(|value| (-10..=10).contains(value))
            .ok_or(InboundError::Fields)?;
        let preferred = fields
            .get("preferred_medium")
            .map_or(Some("text"), Value::as_str)
            .filter(|value| matches!(*value, "text" | "voice"))
            .ok_or(InboundError::Fields)?;
        let initiative = fields
            .get("initiative")
            .map_or(Some(false), boolean)
            .ok_or(InboundError::Fields)?;
        let policy_revision = optional(fields.get("policy_revision"), digest)?;
        let scheduled_day = optional(fields.get("scheduled_day"), calendar_day)?;
        let notification = optional(fields.get("logical_notification_id"), |value| {
            string(Some(value), 0, 200)
        })?;
        let mut normalized = json!({
            "intent_id": intent_id.to_string(), "owner_id": owner_id.to_string(),
            "conversation_id": conversation_id.to_string(), "channel_instance": namespace,
            "form": form, "actions": actions, "attachments": attachments, "expires_at": expires_at,
            "privacy": privacy, "priority": priority, "preferred_medium": preferred, "initiative": initiative,
            "policy_revision": policy_revision, "scheduled_day": scheduled_day, "logical_notification_id": notification,
            "reply_to": reply_to, "replaces": replaces,
        });
        let fields = document.as_object_mut().ok_or(InboundError::Fields)?;
        for name in ["blocks", "evidence_refs"] {
            normalized[name] = fields
                .remove(name)
                .unwrap_or_else(|| Value::Array(Vec::new()));
        }
        Ok(Self {
            intent_id,
            owner_id,
            conversation_id,
            channel,
            document: normalized,
        })
    }

    #[must_use]
    pub fn intent_id(&self) -> Uuid {
        self.intent_id
    }

    #[must_use]
    pub fn owner_id(&self) -> Uuid {
        self.owner_id
    }

    #[must_use]
    pub fn conversation_id(&self) -> Uuid {
        self.conversation_id
    }

    #[must_use]
    pub fn channel(&self) -> &ChannelInstance {
        &self.channel
    }

    #[must_use]
    pub fn document(&self) -> &Value {
        &self.document
    }

    #[must_use]
    pub fn into_document(self) -> Value {
        self.document
    }
}

fn array<'a>(
    fields: &'a Map<String, Value>,
    key: &str,
    max: usize,
) -> Result<&'a [Value], InboundError> {
    match fields.get(key) {
        None => Ok(&[]),
        Some(Value::Array(items)) if items.len() <= max => Ok(items),
        _ => Err(InboundError::Fields),
    }
}

fn form(value: &Value) -> Result<Value, InboundError> {
    let fields = object(value, &["definition_key", "definition_version"])?;
    let key = string(fields.get("definition_key"), 1, 200)?;
    let version = integer(&value["definition_version"])
        .filter(|version| *version >= 1)
        .ok_or(InboundError::Fields)?;
    Ok(json!({"definition_key": key, "definition_version": version}))
}

fn boolean(value: &Value) -> Option<bool> {
    match value {
        Value::Bool(value) => Some(*value),
        Value::Number(_) => match value.as_f64()? {
            0.0 => Some(false),
            1.0 => Some(true),
            _ => None,
        },
        Value::String(value) => match value.to_ascii_lowercase().as_str() {
            "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
            "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
            _ => None,
        },
        _ => None,
    }
}

fn digest(value: &Value) -> Result<&str, InboundError> {
    value
        .as_str()
        .filter(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .ok_or(InboundError::Fields)
}

fn calendar_day(value: &Value) -> Result<String, InboundError> {
    if let Some(date) = value
        .as_str()
        .and_then(|value| speedate::Date::parse_str(value).ok())
        && date.year != 0
    {
        return Ok(date.to_string());
    }
    let datetime = match value {
        Value::String(text) => speedate::DateTime::parse_str(text).ok(),
        Value::Number(_) => crate::event_time::parse_aware_datetime(value),
        _ => None,
    }
    .ok_or(InboundError::Fields)?;
    let time = datetime.time;
    if datetime.date.year == 0
        || (time.hour, time.minute, time.second, time.microsecond) != (0, 0, 0, 0)
    {
        return Err(InboundError::Fields);
    }
    Ok(datetime.date.to_string())
}
