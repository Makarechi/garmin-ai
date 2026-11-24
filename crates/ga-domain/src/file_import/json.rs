mod numbers;
mod strings;

use std::{collections::BTreeSet, mem};

use jiter::{Jiter, JiterError, JiterErrorType, JsonErrorType, Peek};
use serde_json::{Map, Value};

use super::{ImportFileError, MAX_FILE_ROWS, read_text};

const CHAT_NESTING: usize = 1_000;
// Match the relative stack cost of array and object decoding in the pinned
// Python release. The file reader itself uses a bounded heap-backed stack.
// On Linux AMD64 the reference uses roughly 160 bytes for an array frame and
// 272 bytes for an object frame.
#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
const FILE_CONTAINER_BUDGET: usize = 521_900;
#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
const FILE_ARRAY_COST: usize = 10;
#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
const FILE_OBJECT_COST: usize = 17;

#[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
const FILE_CONTAINER_BUDGET: usize = 86_900;
#[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
const FILE_ARRAY_COST: usize = 2;
#[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
const FILE_OBJECT_COST: usize = 3;

#[derive(Clone, Copy)]
enum ContainerLimit {
    File,
    Chat,
}

impl ContainerLimit {
    fn budget(self) -> usize {
        match self {
            Self::File => FILE_CONTAINER_BUDGET,
            Self::Chat => CHAT_NESTING,
        }
    }

    fn cost(self, peek: Peek) -> usize {
        match self {
            Self::File if peek == Peek::Object => FILE_OBJECT_COST,
            Self::File => FILE_ARRAY_COST,
            Self::Chat => 1,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum JsonRowIssue {
    NullCharacter,
    InvalidUnicode,
}

impl JsonRowIssue {
    #[must_use]
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

pub type JsonRow = Result<FileObjectRow, JsonRowIssue>;

pub struct FileObjectRow(Map<String, Value>);

impl FileObjectRow {
    #[must_use]
    pub fn fields(&self) -> &Map<String, Value> {
        &self.0
    }
}

impl From<Map<String, Value>> for FileObjectRow {
    fn from(fields: Map<String, Value>) -> Self {
        Self(fields)
    }
}

impl Drop for FileObjectRow {
    fn drop(&mut self) {
        drop_nested(Value::Object(mem::take(&mut self.0)));
    }
}

/// Decode the whole JSON document before reporting row-local storage issues.
pub fn read_json_rows(bytes: &[u8]) -> Result<Vec<JsonRow>, ImportFileError> {
    let input = strings::escape_unpaired(read_text(bytes)?)?;
    let mut reader = JsonReader::new(input.as_bytes(), ContainerLimit::File);
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
        let mut item = read_value(&mut reader, peek)?;
        count += 1;
        if item.value.is_object() {
            if count <= MAX_FILE_ROWS {
                rows.push(match item.issue {
                    Some(issue) => Err(issue),
                    None => match mem::take(&mut item.value) {
                        Value::Object(object) => Ok(FileObjectRow::from(object)),
                        _ => return Err(ImportFileError::InvalidFormat),
                    },
                });
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

impl Drop for Item {
    fn drop(&mut self) {
        drop_nested(mem::take(&mut self.value));
    }
}

fn drop_nested(value: Value) {
    let mut remaining = match value {
        Value::Array(items) => items,
        Value::Object(fields) => fields.into_values().collect(),
        _ => return,
    };
    while let Some(item) = remaining.pop() {
        match item {
            Value::Array(items) if remaining.is_empty() => remaining = items,
            Value::Array(items) => remaining.extend(items),
            Value::Object(fields) => remaining.extend(fields.into_values()),
            _ => {}
        }
    }
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
    duplicate_key: bool,
    retain_values: bool,
}

impl Frame {
    fn array(retain_values: bool) -> Self {
        Self {
            container: Container::Array(Vec::new()),
            value_issue: None,
            key: String::new(),
            duplicate_key: false,
            retain_values,
        }
    }

    fn object(retain_values: bool) -> Self {
        Self {
            container: Container::Object {
                fields: Map::new(),
                invalid_keys: BTreeSet::new(),
                key_issue: None,
            },
            value_issue: None,
            key: String::new(),
            duplicate_key: false,
            retain_values,
        }
    }

    fn set_key(&mut self, key: &str) {
        self.key.clear();
        self.key.push_str(key);
    }

    fn push(&mut self, mut item: Item) {
        if self.duplicate_key {
            return;
        }
        self.value_issue = item.issue.or(self.value_issue);
        match &mut self.container {
            Container::Array(items) if self.retain_values => items.push(mem::take(&mut item.value)),
            Container::Array(_) => {}
            Container::Object {
                fields,
                invalid_keys,
                key_issue,
            } => {
                let key = std::mem::take(&mut self.key);
                let (valid_key, issue) = strings::decode(&key);
                *key_issue = issue.or(*key_issue);
                if let Some(key) = valid_key {
                    match fields.entry(key.into_owned()) {
                        serde_json::map::Entry::Vacant(field) => {
                            field.insert(if self.retain_values {
                                mem::take(&mut item.value)
                            } else {
                                Value::Null
                            });
                        }
                        serde_json::map::Entry::Occupied(_) => {
                            self.duplicate_key = true;
                        }
                    }
                } else if !invalid_keys.insert(key) {
                    self.duplicate_key = true;
                }
            }
        }
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

    fn finish(mut self) -> Result<Item, ImportFileError> {
        // Python's file import checks duplicate keys after parsing the object.
        if self.duplicate_key {
            return Err(ImportFileError::JsonDuplicateKey);
        }
        if !self.retain_values {
            return Ok(Item {
                value: Value::Null,
                issue: None,
            });
        }
        Ok(match &mut self.container {
            Container::Array(items) => Item {
                value: Value::Array(mem::take(items)),
                issue: self.value_issue,
            },
            Container::Object {
                fields, key_issue, ..
            } => Item {
                value: Value::Object(mem::take(fields)),
                // Python walks values in reverse insertion order, then keys.
                issue: self.value_issue.or(*key_issue),
            },
        })
    }
}

impl Drop for Frame {
    fn drop(&mut self) {
        match &mut self.container {
            Container::Array(items) => drop_nested(Value::Array(mem::take(items))),
            Container::Object { fields, .. } => drop_nested(Value::Object(mem::take(fields))),
        }
    }
}

fn read_value(reader: &mut JsonReader<'_>, first: Peek) -> Result<Item, ImportFileError> {
    let mut stack = Vec::<(Frame, usize)>::new();
    let mut cost = 0;
    let mut peek = first;
    loop {
        let mut item = match peek {
            Peek::Array | Peek::Object => {
                let next_cost = reader.limit.cost(peek);
                if cost + next_cost > reader.limit.budget() {
                    return Err(ImportFileError::InvalidFormat);
                }
                // Finish validation after a duplicate without building its
                // remaining values. Nested objects still track their keys.
                let retain_values = stack
                    .last()
                    .is_none_or(|(frame, _)| frame.retain_values && !frame.duplicate_key);
                let (frame, child) = if peek == Peek::Array {
                    (Frame::array(retain_values), reader.parser.known_array()?)
                } else {
                    let mut frame = Frame::object(retain_values);
                    let child = if let Some(key) = reader.parser.known_object()? {
                        frame.set_key(key);
                        Some(reader.parser.peek()?)
                    } else {
                        None
                    };
                    (frame, child)
                };
                if let Some(child) = child {
                    cost += next_cost;
                    stack.push((frame, next_cost));
                    peek = child;
                    continue;
                }
                frame.finish()?
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
            let Some((frame, _)) = stack.last_mut() else {
                return Ok(item);
            };
            frame.push(item);
            // Preserve the error order for chat answers, whose numeric parser
            // applies its own precision checks while reading each value.
            if frame.duplicate_key && matches!(reader.limit, ContainerLimit::Chat) {
                return Err(ImportFileError::JsonDuplicateKey);
            }
            if let Some(next) = frame.step(&mut reader.parser)? {
                peek = next;
                break;
            }
            let (frame, frame_cost) = stack.pop().ok_or(ImportFileError::InvalidFormat)?;
            cost -= frame_cost;
            item = frame.finish()?;
        }
    }
}

struct JsonReader<'source> {
    input: &'source [u8],
    parser: Jiter<'source>,
    limit: ContainerLimit,
    number_parser: fn(&[u8]) -> Result<serde_json::Number, ImportFileError>,
}

impl<'source> JsonReader<'source> {
    fn new(input: &'source [u8], limit: ContainerLimit) -> Self {
        Self {
            input,
            parser: Jiter::new(input).with_allow_inf_nan(),
            limit,
            number_parser: numbers::parse,
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
            Ok(bytes) => (self.number_parser)(bytes),
            Err(error)
                if error.error_type
                    == JiterErrorType::JsonError(JsonErrorType::NumberOutOfRange) =>
            {
                // The integer digit limit also applies to Jiter's lexical float prefix.
                // Python applies it only to integers, so validate and consume a long float.
                let tail = &self.input[start..];
                let bytes = numbers::long_float(tail)?;
                let value = (self.number_parser)(bytes)?;
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

/// Reuse the strict duplicate-key and Unicode decoder for a single chat JSON answer.
pub(crate) fn read_chat_value(
    text: &str,
    number_parser: fn(&[u8]) -> Result<serde_json::Number, ImportFileError>,
) -> Result<Result<Value, JsonRowIssue>, ImportFileError> {
    let input = strings::escape_unpaired(text)?;
    let mut reader = JsonReader::new(input.as_bytes(), ContainerLimit::Chat);
    reader.number_parser = number_parser;
    let peek = reader.parser.peek()?;
    let mut item = read_value(&mut reader, peek)?;
    reader.parser.finish()?;
    Ok(item
        .issue
        .map_or_else(|| Ok(mem::take(&mut item.value)), Err))
}
