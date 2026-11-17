use std::{collections::HashMap, rc::Rc};

use crate::{
    FitError, FitWireMessage, WireField,
    value::{RawFitValue, decode},
    wire::FieldDefinition,
};

use super::render::unsigned;

const BYTE: u8 = 0x0d;
const FIELD_DESCRIPTION: u16 = 206;
const DEVELOPER_DATA_ID: u16 = 207;
const MAX_METADATA_BYTES: usize = 8 * 1024 * 1024;

pub(super) struct DeveloperField {
    base: u8,
    pub name: Option<String>,
    pub units: Option<String>,
    pub native_field: Option<u8>,
}

impl DeveloperField {
    fn text_bytes(&self) -> usize {
        self.name.as_ref().map_or(0, String::len) + self.units.as_ref().map_or(0, String::len)
    }
}

type FieldTable = HashMap<u8, Rc<DeveloperField>>;
type DefinitionTable = HashMap<(u8, u8), Rc<DeveloperField>>;

#[derive(Default)]
pub(super) struct Developers {
    available: HashMap<u8, FieldTable>,
    retained_bytes: usize,
    definitions: [DefinitionTable; 16],
}

impl Developers {
    pub fn define(&mut self, local: u8, fields: &[FieldDefinition]) {
        let table = &mut self.definitions[usize::from(local)];
        table.clear();
        for field in fields {
            if let Some(index) = field.developer_data_index {
                let available = self.available.entry(index).or_default();
                if let Some(description) = available.get(&field.number) {
                    table.insert((index, field.number), Rc::clone(description));
                }
            }
        }
    }

    pub fn resolve(&self, local: u8, index: u8, number: u8) -> Option<&DeveloperField> {
        self.definitions[usize::from(local)]
            .get(&(index, number))
            .map(Rc::as_ref)
    }

    pub fn decode(
        &self,
        message: &FitWireMessage,
        wire: &WireField,
    ) -> Result<(&'static str, RawFitValue), FitError> {
        if let Some(index) = wire.developer_data_index {
            let base = self
                .resolve(message.local_number, index, wire.number)
                .map_or(BYTE, |f| f.base);
            let result = decode(base, message.byte_order, &wire.bytes)?;
            if result.0 == "byte" && base != BYTE {
                return Err(FitError::InvalidDefinition);
            }
            return Ok(result);
        }
        wire.decode_native(message.byte_order)?
            .ok_or(FitError::InvalidDefinition)
    }

    pub fn update(
        &mut self,
        message: &FitWireMessage,
        fields: &[(&str, RawFitValue)],
    ) -> Result<(), FitError> {
        match message.global_number {
            DEVELOPER_DATA_ID => {
                if let Some(index) = raw_u8(message, fields, 3) {
                    let removed = self.available.insert(index, HashMap::new());
                    self.retained_bytes -= removed.as_ref().map_or(0, |fields| {
                        fields.values().map(|field| field.text_bytes()).sum()
                    });
                }
            }
            FIELD_DESCRIPTION => {
                let (Some(index), Some(number)) =
                    (raw_u8(message, fields, 0), raw_u8(message, fields, 1))
                else {
                    return Ok(());
                };
                let base = raw_u8(message, fields, 2).unwrap_or(BYTE);
                let descriptor = DeveloperField {
                    base,
                    name: raw_text(message, fields, 3),
                    units: raw_text(message, fields, 8),
                    native_field: raw_u8(message, fields, 15),
                };
                let available = self
                    .available
                    .get_mut(&index)
                    .ok_or(FitError::InvalidRecord)?;
                self.retained_bytes += descriptor.text_bytes();
                if let Some(previous) = available.insert(number, Rc::new(descriptor)) {
                    self.retained_bytes -= previous.text_bytes();
                }
                if self.retained_bytes > MAX_METADATA_BYTES {
                    return Err(FitError::LimitExceeded);
                }
            }
            _ => {}
        }
        Ok(())
    }
}

fn native_value<'a>(
    message: &FitWireMessage,
    fields: &'a [(&str, RawFitValue)],
    number: u8,
) -> Option<&'a RawFitValue> {
    message
        .fields
        .iter()
        .zip(fields)
        .find_map(|(wire, (_, raw))| {
            (wire.developer_data_index.is_none() && wire.number == number).then_some(raw)
        })
}

fn raw_u8(message: &FitWireMessage, fields: &[(&str, RawFitValue)], number: u8) -> Option<u8> {
    u8::try_from(unsigned(native_value(message, fields, number)?)?).ok()
}

fn raw_text(
    message: &FitWireMessage,
    fields: &[(&str, RawFitValue)],
    number: u8,
) -> Option<String> {
    match native_value(message, fields, number) {
        Some(RawFitValue::Text(value)) => Some(value.clone()),
        _ => None,
    }
}
