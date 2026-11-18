mod timestamp;

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Number, Value};

use crate::{
    file_import::{
        ImportFileError, JsonRow, JsonRowIssue, decimal::Decimal, read_csv_rows, read_json_rows,
    },
    legacy_json::write_float,
    timestamp::StoredInstant,
};

use super::{DecimalSeparator, FileFormat, FileMapping};

const MAX_NUMERIC_CHARACTERS: usize = 128;
const MAX_NUMERIC_EXPONENT: u64 = 100;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FileFieldInput {
    Boolean,
    Integer,
    Number,
    Text,
    Complex,
}

pub struct FileMappedRow<'a> {
    pub source_id: &'a str,
    pub start: StoredInstant,
    pub end: Option<StoredInstant>,
    pub values: Map<String, Value>,
    pub units: Map<String, Value>,
}

#[derive(Debug, Eq, PartialEq)]
pub struct FileRowIssue<'a> {
    pub field: Option<&'a str>,
    pub code: &'static str,
}

impl From<&'static str> for FileRowIssue<'_> {
    fn from(code: &'static str) -> Self {
        Self { field: None, code }
    }
}

impl FileMapping {
    /// Keep valid rows and redacted row diagnostics in their original file order.
    pub fn read_rows(&self, bytes: &[u8]) -> Result<Vec<JsonRow>, ImportFileError> {
        if self.format() == FileFormat::Json {
            return read_json_rows(bytes);
        }
        let csv = read_csv_rows(bytes)?;
        Ok(csv
            .rows
            .into_iter()
            .map(|row| {
                if csv
                    .headers
                    .iter()
                    .chain(&row)
                    .any(|value| value.contains('\0'))
                {
                    return Err(JsonRowIssue::NullCharacter);
                }
                Ok(csv
                    .headers
                    .iter()
                    .cloned()
                    .zip(row.into_iter().map(Value::String))
                    .collect())
            })
            .collect())
    }

    /// Match the selected form's input types, recording an ID even when its row is invalid.
    pub fn map_row<'a>(
        &'a self,
        raw: &'a JsonRow,
        fields: &BTreeMap<&str, FileFieldInput>,
        seen: &mut BTreeSet<&'a str>,
    ) -> Result<FileMappedRow<'a>, FileRowIssue<'a>> {
        let row = raw.as_ref().map_err(|issue| issue.code())?;
        let source_id = row
            .get(&self.row_id_column)
            .and_then(Value::as_str)
            .filter(|id| (1..=200).contains(&id.chars().count()))
            .ok_or("source_id_required")?;
        if !seen.insert(source_id) {
            return Err("duplicate_source_id".into());
        }
        let start = timestamp::parse(row.get(&self.start_column), &self.timezone)?;
        let end = self
            .end_column
            .as_ref()
            .map(|column| {
                let end = row
                    .get(column)
                    .filter(|value| !value.is_null() && value.as_str() != Some(""))
                    .ok_or("end_required")?;
                let end = timestamp::parse(Some(end), &self.timezone)?;
                (end > start)
                    .then_some(end)
                    .ok_or("end_after_start_required")
            })
            .transpose()?;
        let mut values = Map::new();
        for (name, column) in &self.field_columns {
            let raw = row.get(column).ok_or(FileRowIssue {
                field: Some(name),
                code: "missing_column",
            })?;
            let field = fields.get(name.as_str()).ok_or("invalid_row")?;
            if let Some(converted) = convert(raw, *field, self)? {
                values.insert(name.clone(), converted);
            }
        }
        let units = self
            .units
            .iter()
            .filter(|(name, _)| values.contains_key(name))
            .map(|(name, unit)| (name.clone(), Value::String(unit.clone())))
            .collect();
        Ok(FileMappedRow {
            source_id,
            start,
            end,
            values,
            units,
        })
    }
}

fn convert(
    raw: &Value,
    kind: FileFieldInput,
    mapping: &FileMapping,
) -> Result<Option<Value>, &'static str> {
    if raw.is_null()
        || raw
            .as_str()
            .is_some_and(|text| mapping.null_markers.iter().any(|marker| marker == text))
    {
        return Ok(None);
    }
    let converted = match kind {
        FileFieldInput::Complex => return Err("complex_field_unsupported"),
        FileFieldInput::Boolean => match raw {
            Value::Bool(_) => raw.clone(),
            Value::String(text) if text == "true" || text == "false" => Value::Bool(text == "true"),
            Value::Object(_) | Value::Array(_) => return Err("invalid_row"),
            _ => return Err("boolean_format"),
        },
        FileFieldInput::Integer | FileFieldInput::Number => to_number(raw, kind, mapping)?.into(),
        FileFieldInput::Text => raw.as_str().map(|_| raw.clone()).ok_or("text_format")?,
    };
    Ok(Some(converted))
}

fn to_number(
    raw: &Value,
    kind: FileFieldInput,
    mapping: &FileMapping,
) -> Result<Number, &'static str> {
    let token = match raw {
        Value::String(text)
            if text.chars().take(MAX_NUMERIC_CHARACTERS + 1).count() <= MAX_NUMERIC_CHARACTERS =>
        {
            match mapping.decimal_separator {
                DecimalSeparator::Comma if text.contains('.') => return Err("decimal_separator"),
                DecimalSeparator::Point if text.contains(',') => return Err("decimal_separator"),
                DecimalSeparator::Comma => std::borrow::Cow::Owned(text.replace(',', ".")),
                DecimalSeparator::Point => std::borrow::Cow::Borrowed(text.as_str()),
            }
        }
        Value::Number(number) if number.as_str().len() <= MAX_NUMERIC_CHARACTERS => {
            std::borrow::Cow::Borrowed(number.as_str())
        }
        _ => return Err("numeric_format"),
    };
    let zero_exponent = decimal_syntax(&token).ok_or("numeric_format")?;
    let number = Decimal::new(&token).ok_or("numeric_format")?;
    if number
        .adjusted(zero_exponent)
        .is_none_or(|exponent| exponent.unsigned_abs() > MAX_NUMERIC_EXPONENT)
    {
        return Err("numeric_format");
    }
    if kind == FileFieldInput::Integer {
        return number
            .integer_token()
            .ok_or("integer_format")?
            .parse()
            .map_err(|_| "numeric_format");
    }
    let parsed = token
        .parse::<f64>()
        .ok()
        .filter(|number| number.is_finite())
        .ok_or("numeric_format")?;
    let mut encoded = Vec::with_capacity(32);
    write_float(&mut encoded, parsed).map_err(|_| "numeric_format")?;
    let encoded = std::str::from_utf8(&encoded).map_err(|_| "numeric_format")?;
    if Decimal::new(encoded).ok_or("numeric_format")? != number {
        return Err("numeric_precision");
    }
    encoded.parse().map_err(|_| "numeric_format")
}

fn decimal_syntax(token: &str) -> Option<i64> {
    let token = token.strip_prefix(['+', '-']).unwrap_or(token);
    let (coefficient, exponent) = token.split_once(['e', 'E']).unwrap_or((token, "0"));
    let (integer, fraction) = coefficient.split_once('.').unwrap_or((coefficient, ""));
    if coefficient.is_empty()
        || integer.is_empty() && fraction.is_empty()
        || !integer
            .bytes()
            .chain(fraction.bytes())
            .all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    exponent
        .parse::<i64>()
        .ok()?
        .checked_sub(fraction.len() as i64)
}
