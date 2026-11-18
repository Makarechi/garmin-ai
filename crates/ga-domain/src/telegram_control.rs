//! Commands whose handling stays independent of slow diary and model work.

use crate::channel::{InboundEnvelope, InboundKind};

/// Python's Unicode whitespace set, also used when matching durable control rows.
pub const COMMAND_WHITESPACE: &str = concat!(
    "\u{0009}\u{000a}\u{000b}\u{000c}\u{000d}\u{001c}\u{001d}\u{001e}\u{001f}",
    "\u{0020}\u{0085}\u{00a0}\u{1680}\u{2000}\u{2001}\u{2002}\u{2003}\u{2004}\u{2005}",
    "\u{2006}\u{2007}\u{2008}\u{2009}\u{200a}\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}",
);

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
            .split(|character| COMMAND_WHITESPACE.contains(character))
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
