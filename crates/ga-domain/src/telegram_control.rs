//! Commands whose handling stays independent of slow diary and model work.

use crate::{
    channel::{InboundEnvelope, InboundKind},
    event_payload::python_whitespace,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelegramControl {
    ForgetConversation,
    Today,
    Status,
    Debug,
    Goals,
    Pause,
    Resume,
    Help,
    Start,
}

impl TelegramControl {
    pub fn from_envelope(envelope: &InboundEnvelope) -> Option<Self> {
        if !matches!(envelope.kind(), InboundKind::Text | InboundKind::Voice) {
            return None;
        }
        let mut words = envelope
            .text()?
            .split(python_whitespace)
            .filter(|word| !word.is_empty());
        Some(match words.next()? {
            "/forget_conversation" => Self::ForgetConversation,
            "/today" => Self::Today,
            "/status" => Self::Status,
            "/debug" => Self::Debug,
            "/goals" if envelope.kind() == InboundKind::Text || words.next().is_none() => {
                Self::Goals
            }
            "/pause" => Self::Pause,
            "/resume" => Self::Resume,
            "/help" => Self::Help,
            "/start" => Self::Start,
            _ => return None,
        })
    }
}
