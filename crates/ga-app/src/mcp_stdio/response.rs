use std::{
    io::{self, Write},
    sync::Arc,
};

use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::legacy_json::{Utf8Layout, encode};
use serde::{Serialize, Serializer, ser::SerializeMap};
use serde_json::{Value, json};
use tokio::sync::OwnedSemaphorePermit;

#[derive(Clone, Debug)]
pub(super) enum ToolResult {
    Data(Arc<Document>),
    Error(String),
}

#[derive(Debug)]
pub(super) struct Document {
    json: JsonDocument,
    // Retain read admission until the complete protocol response has been written.
    _permit: Option<OwnedSemaphorePermit>,
}

impl ToolResult {
    pub(super) fn data(json: JsonDocument, permit: Option<OwnedSemaphorePermit>) -> Self {
        Self::Data(Arc::new(Document {
            json,
            _permit: permit,
        }))
    }

    pub(super) fn from_value(
        value: Value,
        permit: Option<OwnedSemaphorePermit>,
    ) -> Result<Self, &'static str> {
        let value = if value.is_object() {
            value
        } else {
            json!({"rows": value})
        };
        let mut spool = JsonSpool::default();
        serde_json::to_writer(&mut spool, &value).map_err(|_| "ValueError")?;
        drop(value);
        let document = spool.finish().map_err(|_| "ValueError")?;
        Ok(Self::data(document, permit))
    }

    pub(super) fn write_json(&self, out: &mut impl Write) -> io::Result<()> {
        match self {
            Self::Data(document) => {
                out.write_all(b"{\"content\":[{\"type\":\"text\",\"text\":\"")?;
                {
                    let mut text = JsonTextEncoder::new(out);
                    io::copy(&mut document.json.reader(), &mut text)?;
                    text.flush()?;
                }
                out.write_all(b"\"}],\"structuredContent\":")?;
                io::copy(&mut document.json.reader(), out)?;
                out.write_all(b",\"isError\":false}")
            }
            Self::Error(_) => serde_json::to_writer(out, self).map_err(io::Error::other),
        }
    }
}

#[derive(Serialize)]
struct TextContent<'a> {
    r#type: &'static str,
    text: &'a str,
}

impl Serialize for ToolResult {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let Self::Error(message) = self else {
            // Data documents are emitted by write_json through the bounded protocol writer.
            return Err(serde::ser::Error::custom(
                "MCP data requires streamed encoding",
            ));
        };
        let mut result = serializer.serialize_map(None)?;
        result.serialize_entry(
            "content",
            &[TextContent {
                r#type: "text",
                text: message,
            }],
        )?;
        result.serialize_entry("isError", &true)?;
        result.end()
    }
}

struct JsonTextEncoder<'a, W> {
    out: &'a mut W,
    quoted: bool,
    escaped: bool,
    number: Vec<u8>,
}

impl<'a, W: Write> JsonTextEncoder<'a, W> {
    fn new(out: &'a mut W) -> Self {
        Self {
            out,
            quoted: false,
            escaped: false,
            number: Vec::new(),
        }
    }

    fn escaped_byte(&mut self, byte: u8) -> io::Result<()> {
        match byte {
            b'"' => self.out.write_all(b"\\\""),
            b'\\' => self.out.write_all(b"\\\\"),
            b'\n' => self.out.write_all(b"\\n"),
            b'\r' => self.out.write_all(b"\\r"),
            b'\t' => self.out.write_all(b"\\t"),
            0..=31 => {
                const HEX: &[u8] = b"0123456789abcdef";
                self.out.write_all(&[
                    b'\\',
                    b'u',
                    b'0',
                    b'0',
                    HEX[usize::from(byte / 16)],
                    HEX[usize::from(byte % 16)],
                ])
            }
            _ => self.out.write_all(&[byte]),
        }
    }

    fn finish_number(&mut self) -> io::Result<()> {
        if self.number.is_empty() {
            return Ok(());
        }
        if self
            .number
            .iter()
            .any(|byte| matches!(byte, b'.' | b'e' | b'E'))
        {
            let encoded = encode(&self.number, Utf8Layout::Ordered, self.number.len())
                .map_err(io::Error::other)?;
            for byte in encoded {
                self.escaped_byte(byte)?;
            }
        } else {
            self.out.write_all(&self.number)?;
        }
        self.number.clear();
        Ok(())
    }
}

impl<W: Write> Write for JsonTextEncoder<'_, W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        for &byte in bytes {
            if !self.number.is_empty() {
                if byte.is_ascii_digit() || matches!(byte, b'e' | b'E' | b'+' | b'-' | b'.') {
                    self.number.push(byte);
                    continue;
                }
                self.finish_number()?;
            }
            if self.quoted {
                self.escaped_byte(byte)?;
                if self.escaped {
                    self.escaped = false;
                } else if byte == b'\\' {
                    self.escaped = true;
                } else if byte == b'"' {
                    self.quoted = false;
                }
            } else {
                match byte {
                    b'"' => {
                        self.quoted = true;
                        self.escaped_byte(byte)?;
                    }
                    b',' | b':' => {
                        self.escaped_byte(byte)?;
                        self.out.write_all(b" ")?;
                    }
                    b'-' | b'0'..=b'9' => self.number.push(byte),
                    b' ' | b'\r' | b'\n' | b'\t' => {}
                    _ => self.escaped_byte(byte)?,
                }
            }
        }
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.finish_number()?;
        self.out.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn streams_the_existing_python_text_and_structured_result()
    -> Result<(), Box<dyn std::error::Error>> {
        let document = json!({
            "text":"Synthetic quote \\\" 🧪,: and newline\n",
            "numbers":[1, -2, 0.00001, 1e20, -0.0, 2.675, true, null],
            "nested":{"empty":[],"value":"é"},
        });
        let compact = serde_json::to_vec(&document)?;
        let expected = String::from_utf8(encode(&compact, Utf8Layout::Ordered, compact.len())?)?;
        let result = ToolResult::from_value(document.clone(), None).map_err(io::Error::other)?;
        let mut bytes = Vec::new();
        result.write_json(&mut bytes)?;
        let wire: Value = serde_json::from_slice(&bytes)?;
        assert_eq!(wire["structuredContent"], document);
        assert_eq!(wire["content"][0]["text"], expected);
        assert_eq!(wire["isError"], false);
        Ok(())
    }

    #[test]
    fn preserves_text_when_json_tokens_cross_read_boundaries()
    -> Result<(), Box<dyn std::error::Error>> {
        let value = json!({"escaped":"\\\"é🧪,:\n","number":1e-5,"negative":-2.675});
        let compact = serde_json::to_vec(&value)?;
        let expected = String::from_utf8(encode(&compact, Utf8Layout::Ordered, compact.len())?)?;
        let mut output = vec![b'"'];
        {
            let mut encoder = JsonTextEncoder::new(&mut output);
            for byte in compact {
                encoder.write_all(&[byte])?;
            }
            encoder.flush()?;
        }
        output.push(b'"');
        assert_eq!(serde_json::from_slice::<String>(&output)?, expected);
        Ok(())
    }
}
