mod developer;
mod render;

use std::{collections::HashMap, io::Read};

use serde_json::{Map, Value, json};

use crate::{
    FitError, FitWireMessage, WireInventory,
    profile::{Component, Field, FitProfile, PROFILE},
    value::RawFitValue,
    wire::{Definition, WireRecord, visit_fit_records},
};

use developer::Developers;
use render::{DisplayField, display, integer, pack_component_value, scale, timestamp, unsigned};

const TIMESTAMP_FIELD: u8 = 253;
const HR_MESSAGE: u16 = 132;
const HR_EVENT_TIMESTAMP: u8 = 9;
const HR_EVENT_TIMESTAMP_12: u8 = 10;

/// One compatible ActivityPart value. The source retains the original wire representation.
pub struct FitProjectionMessage {
    pub frame_index: u32,
    pub message_index: u32,
    pub byte_offset: u64,
    pub name: String,
    pub payload: Value,
}

/// Decode a verified member using the pinned Python profile. All visited rows are
/// candidates until the final sequence checksum and the containing export pass.
pub fn visit_fit_projection(
    source: impl Read,
    max_bytes: u64,
    cancelled: impl FnMut() -> bool,
    mut visit: impl FnMut(FitProjectionMessage) -> Result<(), FitError>,
) -> Result<WireInventory, FitError> {
    let profile = PROFILE.as_ref().map_err(|error| *error)?;
    let mut state = Sequence::default();
    visit_fit_records(source, max_bytes, cancelled, |record| match record {
        WireRecord::Frame => {
            state = Sequence::default();
            Ok(())
        }
        WireRecord::Definition(local, definition) => state.define(profile, local, definition),
        WireRecord::Message(message) => visit(state.project(profile, message)?),
    })
}

#[derive(Default)]
struct Sequence {
    accumulators: HashMap<(u16, u8), u64>,
    developers: Developers,
    last_timestamp: u64,
    hr_start_timestamp: u64,
}

impl Sequence {
    fn define(
        &mut self,
        profile: &FitProfile,
        local: u8,
        definition: &Definition,
    ) -> Result<(), FitError> {
        let message = profile.messages.get(&definition.global_number);
        for wire_field in definition
            .fields
            .iter()
            .filter(|f| f.developer_data_index.is_none())
        {
            let Some(field) = message.and_then(|message| message.fields.get(&wire_field.number))
            else {
                continue;
            };
            for component in field
                .components
                .iter()
                .filter(|component| component.accumulate)
            {
                // The pinned compatibility reader restarts each component when
                // its local definition appears, including another local slot.
                self.accumulators
                    .insert((definition.global_number, component.number), 0);
            }
        }
        self.developers.define(local, &definition.fields);
        Ok(())
    }

    fn project(
        &mut self,
        profile: &FitProfile,
        wire: &FitWireMessage,
    ) -> Result<FitProjectionMessage, FitError> {
        let message = profile.messages.get(&wire.global_number);
        let name = message
            .map(|message| message.name.clone())
            .unwrap_or_else(|| format!("unknown_{}", wire.global_number));
        let raw: Vec<_> = wire
            .fields
            .iter()
            .map(|field| self.developers.decode(wire, field))
            .collect::<Result<_, _>>()?;

        let mut fields = Vec::with_capacity(wire.fields.len() + 1);
        for (wire_field, (base, value)) in wire.fields.iter().zip(&raw) {
            if let Some(index) = wire_field.developer_data_index {
                let descriptor =
                    self.developers
                        .resolve(wire.local_number, index, wire_field.number);
                fields.push(DisplayField::developer(
                    wire_field.number,
                    index,
                    base,
                    value,
                    descriptor,
                ));
                continue;
            }
            let declaration = message.and_then(|msg| msg.fields.get(&wire_field.number));
            if let Some(field) = declaration {
                let field = select_subfield(field, wire, &raw);
                for component in &field.components {
                    if let Some(field) = self.expand_component(
                        profile,
                        wire,
                        &raw,
                        component,
                        value,
                        wire_field.number,
                    )? {
                        fields.push(field);
                    }
                }
                fields.push(display(profile, field, value)?);
            } else {
                fields.push(DisplayField::unknown(wire_field.number, base, value));
            }
            if wire_field.number == TIMESTAMP_FIELD
                && let Some(value) = unsigned(value)
            {
                self.last_timestamp = value;
            } else if wire.global_number == HR_MESSAGE && wire_field.number == HR_EVENT_TIMESTAMP {
                self.hr_start_timestamp = self.last_timestamp;
            }
        }
        if let Some(value) = wire.compressed_timestamp {
            fields.push(DisplayField::compressed_timestamp(value)?);
        }

        if wire.global_number == HR_MESSAGE
            && wire.fields.iter().any(|field| {
                field.number == HR_EVENT_TIMESTAMP_12 && field.developer_data_index.is_none()
            })
        {
            for field in fields
                .iter_mut()
                .filter(|field| field.number == HR_EVENT_TIMESTAMP)
            {
                if let Some(value) = field.value.as_f64() {
                    field.value = timestamp(value)?;
                    field.units = None;
                }
            }
        }

        self.developers.update(wire, &raw)?;
        let mut payload = Map::new();
        for field in &fields {
            if field.developer_index.is_none()
                && let Some(name) = &field.name
            {
                // ActivityPart uses the first occurrence. Its field list still
                // retains both the directly supplied and expanded values.
                payload
                    .entry(name.clone())
                    .or_insert_with(|| field.value.clone());
            }
        }
        payload.insert(
            "_fit".to_owned(),
            json!({
                "global_message_number": wire.global_number,
                "local_message_number": wire.local_number,
                "message_index": wire.message_index,
                "developer_data": wire.developer_data_flag,
                "fields": fields,
            }),
        );
        Ok(FitProjectionMessage {
            frame_index: wire.frame_index,
            message_index: wire.message_index,
            byte_offset: wire.byte_offset,
            name,
            payload: Value::Object(payload),
        })
    }

    fn expand_component(
        &mut self,
        profile: &FitProfile,
        wire: &FitWireMessage,
        decoded: &[(&str, RawFitValue)],
        component: &Component,
        base: &RawFitValue,
        source_number: u8,
    ) -> Result<Option<DisplayField>, FitError> {
        let Some(mut raw) = pack_component_value(base, component)? else {
            return Ok(None);
        };
        if component.accumulate
            && let Some(value) = unsigned(&raw)
        {
            let previous = self
                .accumulators
                .get_mut(&(wire.global_number, component.number))
                .ok_or(FitError::InvalidRecord)?;
            let step = 1_u64 << component.bits;
            let mask = step - 1;
            let base = *previous & !mask;
            let new = base + value + if value < (*previous & mask) { step } else { 0 };
            *previous = new;
            raw = RawFitValue::Unsigned(new);
        }
        let raw = scale(raw, component.scale, component.offset);
        let field = profile
            .messages
            .get(&wire.global_number)
            .and_then(|msg| msg.fields.get(&component.number))
            .ok_or(FitError::InvalidProfile)?;
        let field = select_subfield(field, wire, decoded);
        let mut rendered = render::label(profile, field, &raw);
        if wire.global_number == HR_MESSAGE && source_number == HR_EVENT_TIMESTAMP_12 {
            use RawFitValue::Float;
            if self.hr_start_timestamp == 0 {
                return Err(FitError::MissingHeartRateStart);
            }
            let value = render::number(&rendered).ok_or(FitError::InvalidHeartRateComponent)?;
            rendered = Float(value + self.hr_start_timestamp as f64);
        }
        Ok(Some(DisplayField::projected(field, &raw, rendered)?))
    }
}

fn select_subfield<'a>(
    field: &'a Field,
    wire: &FitWireMessage,
    decoded: &[(&str, RawFitValue)],
) -> &'a Field {
    field
        .subfields
        .iter()
        .find(|variant| {
            variant.references.iter().any(|(number, expected)| {
                wire.fields.iter().zip(decoded).any(|(wire, (_, value))| {
                    wire.developer_data_index.is_none()
                        && wire.number == *number
                        && integer(value) == Some(*expected)
                })
            })
        })
        .unwrap_or(field)
}
