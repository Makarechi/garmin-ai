use std::collections::{BTreeMap, BTreeSet};

use jiter::JsonValue;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::{event_input::known_timezone, legacy_json::write_float, sha256_hex};

use super::ImportFileError;

mod row;
pub use row::{FileFieldInput, FileMappedRow, FileRowIssue};

pub const MAX_MAPPING_BYTES: usize = 16_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FileFormat {
    Csv,
    Json,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DecimalSeparator {
    Point,
    Comma,
}

#[derive(Debug, Eq, PartialEq)]
pub struct FileMapping {
    format: FileFormat,
    source_instance_id: String,
    device_id: String,
    definition_key: String,
    row_id_column: String,
    start_column: String,
    end_column: Option<String>,
    timezone: String,
    field_columns: Vec<(String, String)>,
    units: Vec<(String, String)>,
    decimal_separator: DecimalSeparator,
    null_markers: Vec<String>,
}

type Fields<'a, 'v> = BTreeMap<&'a str, &'a JsonValue<'v>>;

impl FileMapping {
    /// Decode the local mapping file without returning any input values in errors.
    pub fn from_json(bytes: &[u8]) -> Result<Self, ImportFileError> {
        if bytes.len() > MAX_MAPPING_BYTES {
            return Err(ImportFileError::Size);
        }
        let value = JsonValue::parse(bytes, false).map_err(|_| ImportFileError::InvalidMapping)?;
        let JsonValue::Object(object) = &value else {
            return Err(ImportFileError::InvalidMapping);
        };
        let fields: Fields<'_, '_> = object
            .iter()
            .map(|(name, value)| (name.as_ref(), value))
            .collect();
        if fields.keys().any(|key| !KNOWN.contains(key)) {
            return Err(ImportFileError::InvalidMapping);
        }
        let format = match string_field(&fields, "format")? {
            "csv" => FileFormat::Csv,
            "json" => FileFormat::Json,
            _ => return Err(ImportFileError::InvalidMapping),
        };
        let source_instance_id = string_field(&fields, "source_instance_id")?;
        let device_id = string_field(&fields, "device_id")?;
        let definition_key = string_field(&fields, "definition_key")?;
        if !valid_identity(source_instance_id, true)
            || !valid_identity(device_id, true)
            || !definition_key
                .strip_prefix("user.")
                .is_some_and(|key| valid_identity(key, false))
        {
            return Err(ImportFileError::InvalidMapping);
        }
        let timezone = bounded_name(string_field(&fields, "timezone")?)?;
        if !known_timezone(&timezone) {
            return Err(ImportFileError::InvalidMapping);
        }
        let start_column = bounded_name(string_field(&fields, "start_column")?)?;
        let row_id_column = bounded_name(string_field(&fields, "row_id_column")?)?;
        let end_column = match fields.get("end_column") {
            None | Some(JsonValue::Null) => None,
            Some(JsonValue::Str(value)) => Some(bounded_name(value)?),
            Some(_) => return Err(ImportFileError::InvalidMapping),
        };
        let field_columns = string_pairs(fields.get("field_columns").copied(), 1)?;
        if field_columns
            .iter()
            .any(|(_, name)| !(1..=100).contains(&name.chars().count()))
        {
            return Err(ImportFileError::InvalidMapping);
        }
        let units = if let Some(value) = fields.get("units") {
            string_pairs(Some(value), 0)?
        } else {
            Vec::new()
        };
        let decimal_separator = match string_field(&fields, "decimal_separator")? {
            "." => DecimalSeparator::Point,
            "," => DecimalSeparator::Comma,
            _ => return Err(ImportFileError::InvalidMapping),
        };
        let null_markers = null_markers(&fields)?;
        Ok(Self {
            format,
            source_instance_id: source_instance_id.to_owned(),
            device_id: device_id.to_owned(),
            definition_key: definition_key.to_owned(),
            row_id_column,
            start_column,
            end_column,
            timezone,
            field_columns,
            units,
            decimal_separator,
            null_markers,
        })
    }

    #[must_use]
    pub fn format(&self) -> FileFormat {
        self.format
    }

    #[must_use]
    pub fn definition_key(&self) -> &str {
        &self.definition_key
    }

    #[must_use]
    pub fn source_instance(&self) -> &str {
        &self.source_instance_id
    }

    #[must_use]
    pub fn device(&self) -> &str {
        &self.device_id
    }

    #[must_use]
    pub fn timezone(&self) -> &str {
        &self.timezone
    }

    #[must_use]
    pub fn end_column(&self) -> Option<&str> {
        self.end_column.as_deref()
    }

    #[must_use]
    pub fn units(&self) -> &[(String, String)] {
        &self.units
    }

    #[must_use]
    pub fn field_columns(&self) -> &[(String, String)] {
        &self.field_columns
    }

    #[must_use]
    pub fn document(&self) -> Value {
        json!({
            "format": match self.format() { FileFormat::Csv => "csv", FileFormat::Json => "json" },
            "source_instance_id": self.source_instance_id,
            "device_id": self.device_id,
            "definition_key": self.definition_key,
            "row_id_column": self.row_id_column,
            "start_column": self.start_column,
            "end_column": self.end_column,
            "timezone": self.timezone,
            "field_columns": entries(&self.field_columns),
            "units": entries(&self.units),
            "decimal_separator": match self.decimal_separator {
                DecimalSeparator::Point => ".", DecimalSeparator::Comma => ",",
            },
            "null_markers": self.null_markers,
        })
    }

    /// Bind confirmation to the exact file, normalized mapping and recorded contract version.
    pub fn confirmation_hash(
        &self,
        file_sha256: &str,
        schema_sha256: &str,
        definition_version: Uuid,
    ) -> Result<String, ImportFileError> {
        if !valid_digest(file_sha256) || !valid_digest(schema_sha256) {
            return Err(ImportFileError::InvalidMapping);
        }
        let mut plan = self.contract_document(schema_sha256, definition_version);
        plan["file_hash"] = json!(file_sha256);
        document_hash(&plan)
    }

    pub fn contract_hash(
        &self,
        schema_sha256: &str,
        definition_version: Uuid,
    ) -> Result<String, ImportFileError> {
        if !valid_digest(schema_sha256) {
            return Err(ImportFileError::InvalidMapping);
        }
        document_hash(&self.contract_document(schema_sha256, definition_version))
    }

    fn contract_document(&self, schema_sha256: &str, definition_version: Uuid) -> Value {
        json!({"mapping":self.document(),"schema_hash":schema_sha256,
               "definition_version_id":definition_version.to_string()})
    }
}

impl super::FileObjectRow {
    /// Use the same sorted UTF-8 representation for storage and the row receipt.
    pub fn canonical_json(&self) -> Result<String, ImportFileError> {
        let mut encoded = Vec::new();
        write_document(&mut encoded, DocumentValue::Object(self.fields()))?;
        String::from_utf8(encoded).map_err(|_| ImportFileError::InvalidMapping)
    }
}

pub fn document_hash(value: &Value) -> Result<String, ImportFileError> {
    let mut encoded = Vec::new();
    write_document(&mut encoded, DocumentValue::Value(value))?;
    Ok(sha256_hex(&encoded))
}

enum DocumentValue<'a> {
    Value(&'a Value),
    Object(&'a serde_json::Map<String, Value>),
}

enum DocumentItems<'a> {
    Object(std::vec::IntoIter<(&'a String, &'a Value)>),
    Array(std::slice::Iter<'a, Value>),
}

struct DocumentFrame<'a> {
    items: DocumentItems<'a>,
    first: bool,
}

impl<'a> DocumentFrame<'a> {
    fn object(fields: &'a serde_json::Map<String, Value>) -> Self {
        let mut sorted: Vec<_> = fields.iter().collect();
        sorted.sort_unstable_by_key(|(key, _)| key.as_str());
        Self {
            items: DocumentItems::Object(sorted.into_iter()),
            first: true,
        }
    }

    fn array(values: &'a [Value]) -> Self {
        Self {
            items: DocumentItems::Array(values.iter()),
            first: true,
        }
    }

    fn next(&mut self, out: &mut Vec<u8>) -> Result<Option<DocumentValue<'a>>, ImportFileError> {
        let (key, value) = match &mut self.items {
            DocumentItems::Object(items) => {
                if let Some((key, value)) = items.next() {
                    (Some(key), value)
                } else {
                    out.push(b'}');
                    return Ok(None);
                }
            }
            DocumentItems::Array(items) => {
                if let Some(value) = items.next() {
                    (None, value)
                } else {
                    out.push(b']');
                    return Ok(None);
                }
            }
        };
        if !self.first {
            out.push(b',');
        }
        self.first = false;
        if let Some(key) = key {
            serde_json::to_writer(&mut *out, key).map_err(|_| ImportFileError::InvalidMapping)?;
            out.push(b':');
        }
        Ok(Some(DocumentValue::Value(value)))
    }
}

fn write_document(out: &mut Vec<u8>, root: DocumentValue<'_>) -> Result<(), ImportFileError> {
    let invalid = ImportFileError::InvalidMapping;
    let mut pending = Vec::<DocumentFrame<'_>>::new();
    let mut current = root;
    loop {
        match current {
            DocumentValue::Object(fields) | DocumentValue::Value(Value::Object(fields)) => {
                out.push(b'{');
                pending.push(DocumentFrame::object(fields));
            }
            DocumentValue::Value(Value::Array(items)) => {
                out.push(b'[');
                pending.push(DocumentFrame::array(items));
            }
            DocumentValue::Value(Value::Number(number))
                if number.as_str().contains(['.', 'e', 'E']) =>
            {
                write_float(out, number.as_f64().ok_or(invalid)?).map_err(|_| invalid)?;
            }
            DocumentValue::Value(value) => {
                serde_json::to_writer(&mut *out, value).map_err(|_| invalid)?;
            }
        }
        loop {
            let Some(frame) = pending.last_mut() else {
                return Ok(());
            };
            if let Some(next) = frame.next(out)? {
                current = next;
                break;
            }
            pending.pop();
        }
    }
}

const KNOWN: &[&str] = &[
    "format",
    "source_instance_id",
    "device_id",
    "definition_key",
    "row_id_column",
    "start_column",
    "end_column",
    "timezone",
    "field_columns",
    "units",
    "decimal_separator",
    "null_markers",
];

fn string_field<'a>(fields: &Fields<'a, '_>, key: &str) -> Result<&'a str, ImportFileError> {
    match fields.get(key) {
        Some(JsonValue::Str(value)) => Ok(value),
        _ => Err(ImportFileError::InvalidMapping),
    }
}

fn bounded_name(value: &str) -> Result<String, ImportFileError> {
    if (1..=100).contains(&value.chars().count()) {
        Ok(value.to_owned())
    } else {
        Err(ImportFileError::InvalidMapping)
    }
}

fn string_pairs(
    value: Option<&JsonValue<'_>>,
    minimum: usize,
) -> Result<Vec<(String, String)>, ImportFileError> {
    let Some(JsonValue::Object(value)) = value else {
        return Err(ImportFileError::InvalidMapping);
    };
    // Preserve insertion order for field previews and the last value for a repeated key.
    let mut positions = BTreeMap::new();
    let mut pairs = Vec::<(String, &JsonValue<'_>)>::new();
    for (key, value) in value.iter() {
        if let Some(index) = positions.get(key.as_ref()).copied() {
            let (_, target): &mut (String, &JsonValue<'_>) = &mut pairs[index];
            *target = value;
        } else {
            positions.insert(key.as_ref(), pairs.len());
            pairs.push((key.to_string(), value));
        }
    }
    if !(minimum..=32).contains(&pairs.len()) {
        return Err(ImportFileError::InvalidMapping);
    }
    pairs
        .into_iter()
        .map(|(key, value)| match value {
            JsonValue::Str(value) => Ok((key, value.to_string())),
            _ => Err(ImportFileError::InvalidMapping),
        })
        .collect()
}

fn null_markers(fields: &Fields<'_, '_>) -> Result<Vec<String>, ImportFileError> {
    let Some(JsonValue::Array(values)) = fields.get("null_markers") else {
        return Err(ImportFileError::InvalidMapping);
    };
    if values.len() > 16 {
        return Err(ImportFileError::InvalidMapping);
    }
    let mut unique = BTreeSet::new();
    values
        .iter()
        .map(|value| {
            if let JsonValue::Str(value) = value
                && unique.insert(value.as_ref())
            {
                Ok(value.to_string())
            } else {
                Err(ImportFileError::InvalidMapping)
            }
        })
        .collect()
}

fn entries(entries: &[(String, String)]) -> Map<String, Value> {
    entries
        .iter()
        .map(|(key, value)| (key.clone(), json!(value)))
        .collect()
}

fn valid_identity(value: &str, allow_dash: bool) -> bool {
    let bytes = value.as_bytes();
    (1..=63).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes[1..].iter().all(|byte| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || *byte == b'_'
                || (allow_dash && *byte == b'-')
        })
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
