use std::collections::{BTreeMap, BTreeSet};

use jiter::JsonValue;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::{
    event_input::known_timezone,
    legacy_json::{Utf8Layout, encode},
    sha256_hex,
};

use super::ImportFileError;

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

    pub fn format(&self) -> FileFormat {
        self.format
    }

    pub fn field_columns(&self) -> &[(String, String)] {
        &self.field_columns
    }

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
        let plan = json!({
            "file_hash": file_sha256,
            "mapping": self.document(),
            "schema_hash": schema_sha256,
            "definition_version_id": definition_version.to_string(),
        });
        let serialized = serde_json::to_vec(&plan).map_err(|_| ImportFileError::InvalidMapping)?;
        let encoded = encode(&serialized, Utf8Layout::Compact, serialized.len())
            .map_err(|_| ImportFileError::InvalidMapping)?;
        Ok(sha256_hex(&encoded))
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
