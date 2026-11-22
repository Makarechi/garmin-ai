use std::borrow::Cow;

use axum::{
    Json,
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};

use super::tools::{detail, invalid_arguments};

/// Decode a typed FastAPI request before the endpoint validates its fields.
pub(super) fn parse(input: &[u8], headers: &HeaderMap) -> Result<Value, ParseError> {
    if input.is_empty() {
        return Ok(Value::Null);
    }

    let is_json = headers
        .get(header::CONTENT_TYPE)
        .and_then(|header| header.to_str().ok())
        .and_then(|value| value.split(';').next())
        .and_then(|value| value.trim().split_once('/'))
        .is_some_and(|(kind, subtype)| {
            kind.eq_ignore_ascii_case("application")
                && (subtype.eq_ignore_ascii_case("json")
                    || subtype.to_ascii_lowercase().ends_with("+json"))
        });
    if !is_json {
        return str::from_utf8(input)
            .map(|text| Value::String(text.to_owned()))
            .map_err(|_| ParseError::Unsupported);
    }

    let text = decode_json(input).ok_or(ParseError::Encoding)?;
    serde_json::from_str(&text).map_err(|_| {
        let mut scanner = Scanner {
            text: text.as_bytes(),
            offset: 0,
        };
        match scanner.document() {
            Err(ParseError::Syntax { byte, reason }) => ParseError::Syntax {
                byte: text[..byte].chars().count(),
                reason,
            },
            _ => ParseError::Unsupported,
        }
    })
}

pub(super) fn parse_object(input: &[u8], headers: &HeaderMap) -> Result<Value, ParseError> {
    match parse(input, headers)? {
        value @ Value::Object(_) => Ok(value),
        Value::Null => Err(ParseError::MissingBody),
        value => Err(ParseError::ObjectBody(value)),
    }
}

pub(super) fn decode_json(input: &[u8]) -> Option<Cow<'_, str>> {
    match input {
        [0, 0, 0xfe, 0xff, rest @ ..] => utf32(rest, u32::from_be_bytes),
        [0xff, 0xfe, 0, 0, rest @ ..] => utf32(rest, u32::from_le_bytes),
        [0xef, 0xbb, 0xbf, rest @ ..] => str::from_utf8(rest).ok().map(Cow::Borrowed),
        [0xfe, 0xff, rest @ ..] => utf16(rest, u16::from_be_bytes),
        [0xff, 0xfe, rest @ ..] => utf16(rest, u16::from_le_bytes),
        [0, 0, _, _, ..] => utf32(input, u32::from_be_bytes),
        [_, 0, 0, 0, ..] => utf32(input, u32::from_le_bytes),
        [0, _, ..] => utf16(input, u16::from_be_bytes),
        [_, 0, ..] => utf16(input, u16::from_le_bytes),
        _ => str::from_utf8(input).ok().map(Cow::Borrowed),
    }
}

fn utf16(input: &[u8], read: fn([u8; 2]) -> u16) -> Option<Cow<'_, str>> {
    let (units, remainder) = input.as_chunks::<2>();
    if !remainder.is_empty() {
        return None;
    }
    char::decode_utf16(units.iter().copied().map(read))
        .collect::<Result<String, _>>()
        .ok()
        .map(Cow::Owned)
}

fn utf32(input: &[u8], read: fn([u8; 4]) -> u32) -> Option<Cow<'_, str>> {
    let (units, remainder) = input.as_chunks::<4>();
    if !remainder.is_empty() {
        return None;
    }
    units
        .iter()
        .copied()
        .map(read)
        .map(char::from_u32)
        .collect::<Option<String>>()
        .map(Cow::Owned)
}

pub(super) enum ParseError {
    Syntax { byte: usize, reason: &'static str },
    Encoding,
    Unsupported,
    MissingBody,
    ObjectBody(Value),
}

impl IntoResponse for ParseError {
    fn into_response(self) -> Response {
        match self {
            Self::Syntax { byte, reason } => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail": [{
                    "type": "json_invalid", "loc": ["body", byte], "msg": "JSON decode error",
                    "input": {}, "ctx": {"error": reason},
                }]})),
            )
                .into_response(),
            Self::Encoding => detail(
                StatusCode::BAD_REQUEST,
                "There was an error parsing the body",
            ),
            Self::Unsupported => invalid_arguments(),
            Self::MissingBody => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail":[
                    {"type":"missing","loc":["body"],"msg":"Field required","input":null}
                ]})),
            )
                .into_response(),
            Self::ObjectBody(value) => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail":[{
                    "type":"model_attributes_type","loc":["body"],
                    "msg":"Input should be a valid dictionary or object to extract fields from",
                    "input":value
                }]})),
            )
                .into_response(),
        }
    }
}

// Only rejected serde inputs enter this scanner. Match the legacy decoder's
// syntax diagnosis without constructing values or relaxing serde's depth limit.
struct Scanner<'a> {
    text: &'a [u8],
    offset: usize,
}

impl Scanner<'_> {
    fn document(&mut self) -> Result<(), ParseError> {
        self.space();
        self.value(0)?;
        self.space();
        if self.offset != self.text.len() {
            return Err(self.syntax("Extra data"));
        }
        Ok(())
    }

    fn space(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\r' | b'\n')) {
            self.offset += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.text.get(self.offset).copied()
    }

    fn syntax(&self, reason: &'static str) -> ParseError {
        ParseError::Syntax {
            byte: self.offset,
            reason,
        }
    }

    fn consume(&mut self, byte: u8) -> bool {
        if self.peek() == Some(byte) {
            self.offset += 1;
            return true;
        }
        false
    }

    fn value(&mut self, depth: usize) -> Result<(), ParseError> {
        if depth == 128 {
            return Err(ParseError::Unsupported);
        }
        match self.peek() {
            Some(b'{') => self.object(depth + 1),
            Some(b'[') => self.array(depth + 1),
            Some(b'"') => self.string(),
            _ => {
                for literal in [
                    b"null".as_slice(),
                    b"true",
                    b"false",
                    b"NaN",
                    b"Infinity",
                    b"-Infinity",
                ] {
                    if self.text[self.offset..].starts_with(literal) {
                        self.offset += literal.len();
                        return Ok(());
                    }
                }
                if self.number() {
                    Ok(())
                } else {
                    Err(self.syntax("Expecting value"))
                }
            }
        }
    }

    fn object(&mut self, depth: usize) -> Result<(), ParseError> {
        self.offset += 1;
        self.space();
        if self.consume(b'}') {
            return Ok(());
        }
        loop {
            if self.peek() != Some(b'"') {
                return Err(self.syntax("Expecting property name enclosed in double quotes"));
            }
            self.string()?;
            self.space();
            if !self.consume(b':') {
                return Err(self.syntax("Expecting ':' delimiter"));
            }
            self.space();
            self.value(depth)?;
            if self.separator(b'}', "Illegal trailing comma before end of object")? {
                return Ok(());
            }
        }
    }

    fn array(&mut self, depth: usize) -> Result<(), ParseError> {
        self.offset += 1;
        self.space();
        if self.consume(b']') {
            return Ok(());
        }
        loop {
            self.value(depth)?;
            if self.separator(b']', "Illegal trailing comma before end of array")? {
                return Ok(());
            }
        }
    }

    fn separator(&mut self, end: u8, trailing_comma: &'static str) -> Result<bool, ParseError> {
        self.space();
        if self.consume(end) {
            return Ok(true);
        }
        let byte = self.offset;
        if !self.consume(b',') {
            return Err(self.syntax("Expecting ',' delimiter"));
        }
        self.space();
        if self.peek() == Some(end) {
            return Err(ParseError::Syntax {
                byte,
                reason: trailing_comma,
            });
        }
        Ok(false)
    }

    fn string(&mut self) -> Result<(), ParseError> {
        let open = self.offset;
        self.offset += 1;
        while let Some(byte) = self.peek() {
            match byte {
                b'"' => {
                    self.offset += 1;
                    return Ok(());
                }
                b'\\' => self.escape()?,
                0..=31 => return Err(self.syntax("Invalid control character at")),
                _ => self.offset += 1,
            }
        }
        Err(ParseError::Syntax {
            byte: open,
            reason: "Unterminated string starting at",
        })
    }

    fn escape(&mut self) -> Result<(), ParseError> {
        let next = self.offset + 1;
        match self.text.get(next) {
            None => self.offset = next,
            Some(b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't') => {
                self.offset += 2;
            }
            Some(b'u') => {
                let end = next + 5;
                if !self
                    .text
                    .get(next + 1..end)
                    .is_some_and(|digits| digits.iter().all(u8::is_ascii_hexdigit))
                {
                    return Err(ParseError::Syntax {
                        byte: next,
                        reason: "Invalid \\uXXXX escape",
                    });
                }
                self.offset = end;
            }
            _ => return Err(self.syntax("Invalid \\escape")),
        }
        Ok(())
    }

    fn digits(&mut self) {
        while self.peek().is_some_and(|byte| byte.is_ascii_digit()) {
            self.offset += 1;
        }
    }

    fn number(&mut self) -> bool {
        let start = self.offset;
        self.consume(b'-');
        match self.peek() {
            Some(b'0') => self.offset += 1,
            Some(b'1'..=b'9') => self.digits(),
            _ => {
                self.offset = start;
                return false;
            }
        }
        let end_of_integer = self.offset;
        if self.consume(b'.') {
            let fraction_start = self.offset;
            self.digits();
            if fraction_start == self.offset {
                self.offset = end_of_integer;
                return true;
            }
        }
        let end_of_mantissa = self.offset;
        if self.consume(b'e') || self.consume(b'E') {
            if !self.consume(b'+') {
                self.consume(b'-');
            }
            let exponent_start = self.offset;
            self.digits();
            if exponent_start == self.offset {
                self.offset = end_of_mantissa;
            }
        }
        true
    }
}
