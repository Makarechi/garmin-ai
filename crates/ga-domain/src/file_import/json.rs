mod numbers;
mod strings;

use std::collections::BTreeSet;

use jiter::{Jiter, JiterError, JiterErrorType, JsonErrorType, Peek};
use serde_json::{Map, Value};

use super::{ImportFileError, MAX_FILE_ROWS, read_text};

const MAX_NESTING: usize = 1_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum JsonRowIssue {
    NullCharacter,
    InvalidUnicode,
}

impl JsonRowIssue {
    pub fn code(self) -> &'static str {
        match self {
            Self::NullCharacter => "jsonb_null_character",
            Self::InvalidUnicode => "jsonb_invalid_unicode",
        }
    }
}

impl From<JiterError> for ImportFileError {
    fn from(_: JiterError) -> Self {
        Self::InvalidFormat
    }
}

pub type JsonRow = Result<Map<String, Value>, JsonRowIssue>;

/// Decode the whole JSON document before reporting row-local storage issues.
pub fn read_json_rows(bytes: &[u8]) -> Result<Vec<JsonRow>, ImportFileError> {
    let input = strings::escape_unpaired(read_text(bytes)?)?;
    let mut reader = JsonReader::new(input.as_bytes());
    if reader.parser.peek()? != Peek::Array {
        let peek = reader.parser.peek()?;
        read_value(&mut reader, peek)?;
        reader.parser.finish()?;
        return Err(ImportFileError::JsonRoot);
    }

    let mut rows = Vec::new();
    let mut count = 0;
    let mut object_rows = true;
    let mut next = reader.parser.known_array()?;
    while let Some(peek) = next {
        let item = read_value(&mut reader, peek)?;
        count += 1;
        if let Value::Object(object) = item.value {
            if count <= MAX_FILE_ROWS {
                rows.push(item.issue.map_or(Ok(object), Err));
            }
        } else {
            object_rows = false;
        }
        next = reader.parser.array_step()?;
    }
    reader.parser.finish()?;
    if !object_rows || !(1..=MAX_FILE_ROWS).contains(&count) {
        return Err(ImportFileError::RowCount);
    }
    Ok(rows)
}

struct Item {
    value: Value,
    issue: Option<JsonRowIssue>,
}

enum Container {
    Array(Vec<Value>),
    Object {
        fields: Map<String, Value>,
        invalid_keys: BTreeSet<String>,
        key_issue: Option<JsonRowIssue>,
    },
}

struct Frame {
    container: Container,
    value_issue: Option<JsonRowIssue>,
    key: String,
}

impl Frame {
    fn array() -> Self {
        Self {
            container: Container::Array(Vec::new()),
            value_issue: None,
            key: String::new(),
        }
    }

    fn object() -> Self {
        Self {
            container: Container::Object {
                fields: Map::new(),
                invalid_keys: BTreeSet::new(),
                key_issue: None,
            },
            value_issue: None,
            key: String::new(),
        }
    }

    fn set_key(&mut self, key: &str) {
        self.key.clear();
        self.key.push_str(key);
    }

    fn push(&mut self, item: Item) -> Result<(), ImportFileError> {
        self.value_issue = item.issue.or(self.value_issue);
        match &mut self.container {
            Container::Array(items) => items.push(item.value),
            Container::Object {
                fields,
                invalid_keys,
                key_issue,
            } => {
                let key = std::mem::take(&mut self.key);
                let (valid_key, issue) = strings::decode(&key);
                *key_issue = issue.or(*key_issue);
                let repeated = match valid_key {
                    Some(key) => fields.insert(key.into_owned(), item.value).is_some(),
                    None => !invalid_keys.insert(key),
                };
                if repeated {
                    return Err(ImportFileError::JsonDuplicateKey);
                }
            }
        }
        Ok(())
    }

    fn step(&mut self, parser: &mut Jiter<'_>) -> Result<Option<Peek>, ImportFileError> {
        match self.container {
            Container::Array(_) => Ok(parser.array_step()?),
            Container::Object { .. } => match parser.next_key()? {
                Some(key) => {
                    self.set_key(key);
                    Ok(Some(parser.peek()?))
                }
                None => Ok(None),
            },
        }
    }

    fn finish(self) -> Item {
        match self.container {
            Container::Array(items) => Item {
                value: Value::Array(items),
                issue: self.value_issue,
            },
            Container::Object {
                fields, key_issue, ..
            } => Item {
                value: Value::Object(fields),
                // Python walks values in reverse insertion order, then keys.
                issue: self.value_issue.or(key_issue),
            },
        }
    }
}

fn read_value(reader: &mut JsonReader<'_>, first: Peek) -> Result<Item, ImportFileError> {
    let mut stack = Vec::<Frame>::new();
    let mut peek = first;
    loop {
        let mut item = match peek {
            Peek::Array | Peek::Object => {
                if stack.len() == MAX_NESTING {
                    return Err(ImportFileError::InvalidFormat);
                }
                let (frame, child) = if peek == Peek::Array {
                    (Frame::array(), reader.parser.known_array()?)
                } else {
                    let mut frame = Frame::object();
                    let child = if let Some(key) = reader.parser.known_object()? {
                        frame.set_key(key);
                        Some(reader.parser.peek()?)
                    } else {
                        None
                    };
                    (frame, child)
                };
                if let Some(child) = child {
                    stack.push(frame);
                    peek = child;
                    continue;
                }
                frame.finish()
            }
            Peek::String => {
                let (text, issue) = strings::decode(reader.parser.known_str()?);
                Item {
                    value: text.map_or(Value::Null, |text| Value::String(text.into_owned())),
                    issue,
                }
            }
            _ => Item {
                value: reader.scalar(peek)?,
                issue: None,
            },
        };

        loop {
            let Some(frame) = stack.last_mut() else {
                return Ok(item);
            };
            frame.push(item)?;
            if let Some(next) = frame.step(&mut reader.parser)? {
                peek = next;
                break;
            }
            item = stack.pop().ok_or(ImportFileError::InvalidFormat)?.finish();
        }
    }
}

struct JsonReader<'source> {
    input: &'source [u8],
    parser: Jiter<'source>,
}

impl<'source> JsonReader<'source> {
    fn new(input: &'source [u8]) -> Self {
        Self {
            input,
            parser: Jiter::new(input).with_allow_inf_nan(),
        }
    }

    fn scalar(&mut self, peek: Peek) -> Result<Value, ImportFileError> {
        match peek {
            Peek::Null => {
                self.parser.known_null()?;
                Ok(Value::Null)
            }
            Peek::True | Peek::False => Ok(Value::Bool(self.parser.known_bool(peek)?)),
            _ if peek.is_num() => self.number(peek).map(Value::Number),
            _ => Err(ImportFileError::InvalidFormat),
        }
    }

    fn number(&mut self, peek: Peek) -> Result<serde_json::Number, ImportFileError> {
        let start = self.parser.current_index();
        match self.parser.known_number_bytes(peek) {
            Ok(bytes) => numbers::parse(bytes),
            Err(error)
                if error.error_type
                    == JiterErrorType::JsonError(JsonErrorType::NumberOutOfRange) =>
            {
                // The integer digit limit also applies to Jiter's lexical float prefix.
                // Python applies it only to integers, so validate and consume a long float.
                let tail = &self.input[start..];
                let bytes = numbers::long_float(tail)?;
                let value = numbers::parse(bytes)?;
                let rest = &tail[bytes.len()..];
                let parser = std::mem::replace(&mut self.parser, Jiter::new(rest));
                self.parser = Jiter::with_tape(rest, parser.into_tape()).with_allow_inf_nan();
                self.input = rest;
                Ok(value)
            }
            Err(error) => Err(error.into()),
        }
    }
}
