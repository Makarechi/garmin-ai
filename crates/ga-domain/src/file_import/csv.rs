use std::collections::BTreeSet;

use super::{ImportFileError, MAX_FILE_ROWS, read_text};

const MAX_FIELD_CHARACTERS: usize = 131_072;

#[derive(Debug, Eq, PartialEq)]
pub struct CsvRows {
    pub headers: Vec<String>,
    pub rows: Vec<Vec<String>>,
}

pub fn read_csv_rows(bytes: &[u8]) -> Result<CsvRows, ImportFileError> {
    let mut reader = CsvReader {
        remaining: read_text(bytes)?,
    };
    let headers = reader.row()?.ok_or(ImportFileError::Headers)?;
    if headers.is_empty() || headers.iter().collect::<BTreeSet<_>>().len() != headers.len() {
        return Err(ImportFileError::Headers);
    }
    let mut rows = Vec::new();
    while rows.len() <= MAX_FILE_ROWS {
        let Some(row) = reader.row()? else {
            break;
        };
        if !row.is_empty() {
            rows.push(row);
        }
    }
    if rows.iter().any(|row| row.len() != headers.len()) {
        return Err(ImportFileError::RowWidth);
    }
    if !(1..=MAX_FILE_ROWS).contains(&rows.len()) {
        return Err(ImportFileError::RowCount);
    }
    Ok(CsvRows { headers, rows })
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum FieldState {
    Start,
    Unquoted,
    Quoted,
    QuoteClosed,
}

struct CsvReader<'a> {
    remaining: &'a str,
}

impl CsvReader<'_> {
    fn row(&mut self) -> Result<Option<Vec<String>>, ImportFileError> {
        if self.remaining.is_empty() {
            return Ok(None);
        }
        use FieldState::{QuoteClosed, Quoted, Start, Unquoted};
        let mut state = Start;
        let mut row = Vec::new();
        let mut field = String::new();
        for (index, character) in self.remaining.char_indices() {
            match (state, character) {
                (Start, '"') => state = Quoted,
                (Quoted, '"') => state = QuoteClosed,
                (Quoted, character) => field.push(character),
                (QuoteClosed, '"') => {
                    field.push('"');
                    state = Quoted;
                }
                (_, ',') => {
                    push_field(&mut row, std::mem::take(&mut field))?;
                    state = Start;
                }
                (_, '\r' | '\n') => {
                    if state != Start || !row.is_empty() {
                        push_field(&mut row, field)?;
                    }
                    let tail = &self.remaining[index + 1..];
                    self.remaining = if character == '\r' {
                        tail.strip_prefix('\n').unwrap_or(tail)
                    } else {
                        tail
                    };
                    return Ok(Some(row));
                }
                (QuoteClosed, _) => return Err(ImportFileError::InvalidFormat),
                (_, character) => {
                    field.push(character);
                    state = Unquoted;
                }
            }
        }
        if state == Quoted {
            return Err(ImportFileError::InvalidFormat);
        }
        push_field(&mut row, field)?;
        self.remaining = "";
        Ok(Some(row))
    }
}

fn push_field(row: &mut Vec<String>, value: String) -> Result<(), ImportFileError> {
    if value.len() > MAX_FIELD_CHARACTERS && value.chars().count() > MAX_FIELD_CHARACTERS {
        return Err(ImportFileError::InvalidFormat);
    }
    row.push(value);
    Ok(())
}
