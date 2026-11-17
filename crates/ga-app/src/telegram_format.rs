//! Render inline Markdown in the same text and entity format as the Telegram sender.

mod inline;

use std::ops::Range;

use markdown_it_rust_core::AttrVal;
use serde_json::{Value, json};

const PART_LIMIT: usize = 3500;

#[derive(Debug, PartialEq, Eq)]
pub struct MessagePart {
    pub text: String,
    pub entities: Vec<TextEntity>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TextEntity {
    pub kind: &'static str,
    pub offset: usize,
    pub length: usize,
    pub url: Option<String>,
}

impl TextEntity {
    pub fn as_json(&self) -> Value {
        let mut object = json!({
            "type": self.kind,
            "offset": self.offset,
            "length": self.length,
        });
        if let Some(url) = &self.url {
            object["url"] = json!(url);
        }
        object
    }
}

#[derive(Clone)]
struct EntityRange {
    kind: &'static str,
    units: Range<usize>,
    url: Option<String>,
}

pub fn message_parts(source: &str) -> Vec<MessagePart> {
    let normalized = source
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\0', "\u{fffd}");
    let mut text = String::new();
    let mut entities = Vec::new();
    let mut open = Vec::new();
    let mut position = 0;
    for token in inline::parse(&normalized) {
        let token = token.borrow();
        let name = token
            .typ
            .strip_suffix("_open")
            .or_else(|| token.typ.strip_suffix("_close"));
        let style = match name {
            Some("strong") => Some("bold"),
            Some("em") => Some("italic"),
            Some("s") => Some("strikethrough"),
            Some("link") => Some("text_link"),
            _ => None,
        };
        if let Some(kind) = style {
            if token.nesting == 1 {
                let url = match token.attr_get("href") {
                    Some(AttrVal::Str(url)) => Some(url),
                    _ => None,
                };
                open.push(EntityRange {
                    kind,
                    units: position..position,
                    url,
                });
            } else if let Some(mut entity) = open.pop()
                && entity.units.start < position
            {
                entity.units.end = position;
                entities.push(entity);
            }
            continue;
        }
        let content = match token.typ.as_str() {
            "softbreak" | "hardbreak" => "\n",
            _ => &token.content,
        };
        let end = position + content.encode_utf16().count();
        if token.typ == "code_inline" && position < end {
            entities.push(EntityRange {
                kind: "code",
                units: position..end,
                url: None,
            });
        }
        text.push_str(content);
        position = end;
    }
    let text = if text.is_empty() { source } else { &text };
    let ranges = exclude_code(&entities);
    let mut parts = Vec::new();
    let (mut start_byte, mut start_unit, mut width) = (0, 0, 0);
    for (byte, character) in text.char_indices() {
        let next = character.len_utf16();
        if width + next > PART_LIMIT {
            parts.push(part(&text[start_byte..byte], start_unit, width, &ranges));
            start_byte = byte;
            start_unit += width;
            width = 0;
        }
        width += next;
    }
    if width != 0 {
        parts.push(part(&text[start_byte..], start_unit, width, &ranges));
    }
    parts
}

fn exclude_code(entities: &[EntityRange]) -> Vec<EntityRange> {
    // Code ranges are recorded in text order. Other entities are kept in token-close order.
    let code: Vec<_> = entities
        .iter()
        .filter(|entity| entity.kind == "code")
        .map(|entity| &entity.units)
        .collect();
    let mut result = Vec::new();
    for entity in entities {
        if entity.kind == "code" {
            result.push(entity.clone());
            continue;
        }
        let mut start = entity.units.start;
        let end = entity.units.end;
        let first = code.partition_point(|units| units.end <= start);
        for units in &code[first..] {
            if units.start >= end {
                break;
            }
            if start < units.start {
                result.push(EntityRange {
                    units: start..units.start,
                    ..entity.clone()
                });
            }
            start = units.end;
        }
        if start < end {
            result.push(EntityRange {
                units: start..end,
                ..entity.clone()
            });
        }
    }
    result
}

fn part(text: &str, start: usize, width: usize, ranges: &[EntityRange]) -> MessagePart {
    let entities = ranges
        .iter()
        .filter_map(|entity| {
            let left = entity.units.start.max(start);
            let right = entity.units.end.min(start + width);
            if left >= right {
                return None;
            }
            Some(TextEntity {
                kind: entity.kind,
                offset: left - start,
                length: right - left,
                url: entity.url.clone(),
            })
        })
        .collect();
    MessagePart {
        text: text.to_owned(),
        entities,
    }
}
