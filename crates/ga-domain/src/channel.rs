mod outbound;
pub use outbound::{OutboundError, OutboundIntent};

use std::{error::Error, fmt};

use serde_json::{Map, Value, json};
use time::{OffsetDateTime, UtcOffset};
use uuid::Uuid;

use crate::timestamp::StoredInstant;

const MAX_ENVELOPE_BYTES: usize = 2 * 1024 * 1024;
const TELEGRAM_NAMESPACE: Uuid = Uuid::from_u128(0x5ddd62fc_6890_44b6_86a2_20f77524378f);

pub fn telegram_conversation_id(owner: Uuid, instance: &str, chat: impl fmt::Display) -> Uuid {
    Uuid::new_v5(
        &TELEGRAM_NAMESPACE,
        format!("{owner}:telegram:{instance}:{chat}").as_bytes(),
    )
}

pub fn legacy_telegram_action_id(update_id: i64) -> Uuid {
    Uuid::new_v5(
        &TELEGRAM_NAMESPACE,
        format!("action:{update_id}").as_bytes(),
    )
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InboundError {
    Size,
    Document,
    Fields,
    Payload,
}

impl fmt::Display for InboundError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Size => "Inbound message envelope exceeds the size limit",
            Self::Document => "Inbound message envelope has invalid JSON",
            Self::Fields => "Inbound message envelope has invalid fields",
            Self::Payload => "Inbound message kind requires its matching payload",
        })
    }
}
impl Error for InboundError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InboundKind {
    Text,
    Voice,
    Action,
    Edit,
    Receipt,
    System,
}

impl InboundKind {
    fn parse(value: &Value) -> Option<Self> {
        Some(match value.as_str()? {
            "text" => Self::Text,
            "voice" => Self::Voice,
            "action" => Self::Action,
            "edit" => Self::Edit,
            "receipt" => Self::Receipt,
            "system" => Self::System,
            _ => return None,
        })
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Voice => "voice",
            Self::Action => "action",
            Self::Edit => "edit",
            Self::Receipt => "receipt",
            Self::System => "system",
        }
    }
}

/// An opaque identity belongs to its exact channel and integration instance.
pub struct ChannelInstance {
    channel: String,
    instance_id: String,
}

impl ChannelInstance {
    fn parse(value: &Value) -> Result<Self, InboundError> {
        let fields = object(value, &["channel", "instance_id"])?;
        let channel = string(fields.get("channel"), 1, 100)?.to_owned();
        let instance_id = string(fields.get("instance_id"), 1, 200)?.to_owned();
        Ok(Self {
            channel,
            instance_id,
        })
    }

    pub fn channel(&self) -> &str {
        &self.channel
    }
    pub fn instance_id(&self) -> &str {
        &self.instance_id
    }

    fn document(&self) -> Value {
        json!({"channel": self.channel, "instance_id": self.instance_id})
    }
}

/// Parse the normalized envelope supplied by an authenticated transport adapter.
/// The adapter must verify sender and owner before submitting the envelope to storage.
pub struct InboundEnvelope {
    message_id: Uuid,
    owner_id: Uuid,
    conversation_id: Uuid,
    channel: ChannelInstance,
    external_event_id: String,
    external_message_id: Option<String>,
    sender_ref: String,
    occurred_at: Option<OffsetDateTime>,
    received_at: OffsetDateTime,
    time_precision: &'static str,
    kind: InboundKind,
    text: Option<String>,
    reply_to: Option<Value>,
    attachments: Vec<Value>,
    action: Option<(Uuid, Value)>,
    revision: i32,
}

impl InboundEnvelope {
    pub fn parse(bytes: &[u8]) -> Result<Self, InboundError> {
        if bytes.len() > MAX_ENVELOPE_BYTES {
            return Err(InboundError::Size);
        }
        let body: Value = serde_json::from_slice(bytes).map_err(|_| InboundError::Document)?;
        let fields = object(
            &body,
            &[
                "message_id",
                "owner_id",
                "conversation_id",
                "channel_instance",
                "external_event_id",
                "external_message_id",
                "sender_ref",
                "occurred_at",
                "received_at",
                "time_precision",
                "kind",
                "text",
                "reply_to",
                "attachments",
                "action",
                "revision",
            ],
        )?;
        let message_id = fields
            .get("message_id")
            .map_or_else(|| Ok(Uuid::new_v4()), uuid)?;
        let owner_id = uuid(&body["owner_id"])?;
        let conversation_id = uuid(&body["conversation_id"])?;
        let channel = ChannelInstance::parse(&body["channel_instance"])?;
        let external_event_id = string(fields.get("external_event_id"), 1, 1000)?.to_owned();
        let external_message_id = optional(fields.get("external_message_id"), |value| {
            string(Some(value), 1, 1000).map(str::to_owned)
        })?;
        let sender_ref = string(fields.get("sender_ref"), 1, 1000)?.to_owned();
        let occurred_at = optional(fields.get("occurred_at"), datetime)?;
        let received_at = datetime(&body["received_at"])?;
        let time_precision = match fields.get("time_precision").map(Value::as_str) {
            None | Some(Some("unknown")) => "unknown",
            Some(Some("exact")) => "exact",
            Some(Some("second")) => "second",
            Some(Some("minute")) => "minute",
            Some(Some("date")) => "date",
            _ => return Err(InboundError::Fields),
        };
        let kind = InboundKind::parse(&body["kind"]).ok_or(InboundError::Fields)?;
        let text = optional(fields.get("text"), |value| {
            string(Some(value), 0, 100_000).map(str::to_owned)
        })?;
        let reply_to = optional(fields.get("reply_to"), message_ref)?;
        let attachments = match fields.get("attachments") {
            Some(Value::Array(values)) if values.len() <= 20 => {
                values.iter().map(attachment).collect::<Result<_, _>>()?
            }
            None => Vec::new(),
            _ => return Err(InboundError::Fields),
        };
        let action = optional(fields.get("action"), action)?;
        let revision = match fields.get("revision") {
            Some(value) => integer(value)
                .and_then(|value| i32::try_from(value).ok())
                .filter(|revision| *revision >= 1)
                .ok_or(InboundError::Fields)?,
            None => 1,
        };
        if (kind == InboundKind::Action && action.is_none())
            || (matches!(kind, InboundKind::Text | InboundKind::Voice) && text.is_none())
        {
            return Err(InboundError::Payload);
        }
        Ok(Self {
            message_id,
            owner_id,
            conversation_id,
            channel,
            external_event_id,
            external_message_id,
            sender_ref,
            occurred_at,
            received_at,
            time_precision,
            kind,
            text,
            reply_to,
            attachments,
            action,
            revision,
        })
    }

    pub fn message_id(&self) -> Uuid {
        self.message_id
    }
    pub fn owner_id(&self) -> Uuid {
        self.owner_id
    }
    pub fn conversation_id(&self) -> Uuid {
        self.conversation_id
    }
    pub fn channel(&self) -> &ChannelInstance {
        &self.channel
    }
    pub fn external_event_id(&self) -> &str {
        &self.external_event_id
    }
    pub fn external_message_id(&self) -> Option<&str> {
        self.external_message_id.as_deref()
    }
    pub fn sender_ref(&self) -> &str {
        &self.sender_ref
    }
    pub fn occurred_at(&self) -> Option<OffsetDateTime> {
        self.occurred_at
    }
    pub fn received_at(&self) -> OffsetDateTime {
        self.received_at
    }
    pub fn kind(&self) -> InboundKind {
        self.kind
    }
    pub fn text(&self) -> Option<&str> {
        self.text.as_deref()
    }
    pub fn revision(&self) -> i32 {
        self.revision
    }
    pub fn action_operation(&self) -> Option<Uuid> {
        self.action.as_ref().map(|(id, _)| *id)
    }

    pub fn document(&self) -> Value {
        json!({
            "message_id": self.message_id.to_string(), "owner_id": self.owner_id.to_string(), "channel_instance": self.channel.document(),
            "conversation_id": self.conversation_id.to_string(), "external_event_id": self.external_event_id,
            "external_message_id": self.external_message_id, "sender_ref": self.sender_ref,
            "occurred_at": self.occurred_at.map(datetime_text), "received_at": datetime_text(self.received_at),
            "time_precision": self.time_precision, "kind": self.kind.label(), "text": self.text, "reply_to": self.reply_to,
            "attachments": self.attachments, "action": self.action.as_ref().map(|(_, action)| action), "revision": self.revision,
        })
    }
}

fn object<'a>(value: &'a Value, keys: &[&str]) -> Result<&'a Map<String, Value>, InboundError> {
    value
        .as_object()
        .filter(|value| value.keys().all(|key| keys.contains(&key.as_str())))
        .ok_or(InboundError::Fields)
}

fn string(value: Option<&Value>, min: usize, max: usize) -> Result<&str, InboundError> {
    value
        .and_then(Value::as_str)
        .filter(|text| (min..=max).contains(&text.chars().count()))
        .ok_or(InboundError::Fields)
}

fn optional<'a, T>(
    value: Option<&'a Value>,
    parse: impl FnOnce(&'a Value) -> Result<T, InboundError>,
) -> Result<Option<T>, InboundError> {
    value
        .filter(|value| !value.is_null())
        .map(parse)
        .transpose()
}

fn uuid(value: &Value) -> Result<Uuid, InboundError> {
    value
        .as_str()
        .and_then(|value| value.parse().ok())
        .ok_or(InboundError::Fields)
}

fn datetime(value: &Value) -> Result<OffsetDateTime, InboundError> {
    StoredInstant::legacy_input_with_offset(value).ok_or(InboundError::Fields)
}

fn datetime_text(at: OffsetDateTime) -> String {
    let mut text = format!(
        "{}T{:02}:{:02}:{:02}",
        at.date(),
        at.hour(),
        at.minute(),
        at.second()
    );
    if at.microsecond() != 0 {
        text.push_str(&format!(".{:06}", at.microsecond()));
    }
    if at.offset() == UtcOffset::UTC {
        text.push('Z');
    } else {
        let seconds = at.offset().whole_seconds();
        let sign = if seconds < 0 { '-' } else { '+' };
        let minutes = seconds.unsigned_abs() / 60;
        text.push_str(&format!("{sign}{:02}:{:02}", minutes / 60, minutes % 60));
    }
    text
}

fn integer(value: &Value) -> Option<i64> {
    if let Some(value) = value.as_i64() {
        return Some(value);
    }
    match value {
        Value::Bool(value) => Some(i64::from(*value)),
        Value::String(value) => {
            let value = value.trim();
            let integer = match value.split_once('.') {
                Some((integer, fraction))
                    if !fraction.is_empty() && fraction.bytes().all(|byte| byte == b'0') =>
                {
                    integer
                }
                Some(_) => return None,
                None => value,
            };
            integer.parse().ok()
        }
        Value::Number(_) => {
            let number = value.as_f64()?;
            (number.fract() == 0.0
                && (-9_223_372_036_854_775_808.0..9_223_372_036_854_775_808.0).contains(&number))
            .then_some(number as i64)
        }
        _ => None,
    }
}

fn message_ref(value: &Value) -> Result<Value, InboundError> {
    let fields = object(value, &["channel_instance", "external_message_id"])?;
    let channel = ChannelInstance::parse(&value["channel_instance"])?;
    let id = string(fields.get("external_message_id"), 1, 1000)?;
    Ok(json!({"channel_instance": channel.document(), "external_message_id": id}))
}

fn attachment(value: &Value) -> Result<Value, InboundError> {
    let fields = object(
        value,
        &[
            "attachment_id",
            "kind",
            "media_type",
            "filename",
            "size_bytes",
            "external_id",
        ],
    )?;
    let id = fields
        .get("attachment_id")
        .map_or_else(|| Ok(Uuid::new_v4()), uuid)?;
    let kind = string(fields.get("kind"), 1, 100)?;
    let media_type = optional(fields.get("media_type"), |value| {
        string(Some(value), 1, 200)
    })?;
    let filename = optional(fields.get("filename"), |value| string(Some(value), 1, 500))?;
    let size = optional(fields.get("size_bytes"), |value| {
        integer(value)
            .filter(|value| *value >= 0)
            .ok_or(InboundError::Fields)
    })?;
    let external = optional(fields.get("external_id"), |value| {
        string(Some(value), 1, 1000)
    })?;
    Ok(
        json!({"attachment_id": id.to_string(), "kind": kind, "media_type": media_type, "filename": filename, "size_bytes": size, "external_id": external}),
    )
}

fn action(value: &Value) -> Result<(Uuid, Value), InboundError> {
    let fields = object(
        value,
        &["action_id", "label", "operation_id", "token", "expires_at"],
    )?;
    let id = string(fields.get("action_id"), 1, 200)?;
    let label = string(fields.get("label"), 1, 300)?;
    let operation = uuid(&value["operation_id"])?;
    let token = optional(fields.get("token"), |value| string(Some(value), 16, 500))?;
    let expires = optional(fields.get("expires_at"), datetime)?.map(datetime_text);
    Ok((
        operation,
        json!({"action_id": id, "label": label, "operation_id": operation.to_string(), "token": token, "expires_at": expires}),
    ))
}
