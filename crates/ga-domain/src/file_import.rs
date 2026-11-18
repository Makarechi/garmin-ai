mod csv;
mod decimal;
mod json;
mod mapping;

pub use csv::{CsvRows, read_csv_rows};
pub use json::{JsonRow, JsonRowIssue, read_json_rows};
pub use mapping::{
    FileFieldInput, FileFormat, FileMappedRow, FileMapping, FileRowIssue, MAX_MAPPING_BYTES,
};

use std::{error::Error, fmt};

pub const MAX_FILE_BYTES: usize = 512_000;
pub const MAX_FILE_ROWS: usize = 500;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ImportFileError {
    Size,
    Utf8,
    InvalidFormat,
    InvalidMapping,
    Headers,
    RowWidth,
    RowCount,
    JsonRoot,
    JsonDuplicateKey,
    JsonNonfinite,
    JsonFloatOverflow,
    JsonPrecision,
}

impl fmt::Display for ImportFileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Size => "File or mapping exceeds the local size limit",
            Self::Utf8 => "File must use UTF-8",
            Self::InvalidFormat => "Invalid CSV or JSON file",
            Self::InvalidMapping => "Invalid mapping file",
            Self::Headers => "CSV headers must be unique",
            Self::RowWidth => "CSV rows must match the header width",
            Self::RowCount => "File must contain 1 to 500 object rows",
            Self::JsonRoot => "JSON root must be a list",
            Self::JsonDuplicateKey => "Duplicate JSON key",
            Self::JsonNonfinite => "Non-finite JSON value",
            Self::JsonFloatOverflow => "non-finite JSON value",
            Self::JsonPrecision => "JSON number exceeds the supported precision",
        })
    }
}
impl Error for ImportFileError {}

fn read_text(bytes: &[u8]) -> Result<&str, ImportFileError> {
    if bytes.len() > MAX_FILE_BYTES {
        return Err(ImportFileError::Size);
    }
    let bytes = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(bytes);
    std::str::from_utf8(bytes).map_err(|_| ImportFileError::Utf8)
}
