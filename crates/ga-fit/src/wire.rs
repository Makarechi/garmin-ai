use std::io::Read;

use crc::Digest;

use crate::{
    FIT_CRC, FitError, FitInventory, MAX_FIT_BYTES, check_cancelled, frame_end, read_frame_header,
    verify_checksum,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ByteOrder {
    Little,
    Big,
}

impl ByteOrder {
    fn u16(self, bytes: [u8; 2]) -> u16 {
        match self {
            Self::Little => u16::from_le_bytes(bytes),
            Self::Big => u16::from_be_bytes(bytes),
        }
    }

    fn u32(self, bytes: [u8; 4]) -> u32 {
        match self {
            Self::Little => u32::from_le_bytes(bytes),
            Self::Big => u32::from_be_bytes(bytes),
        }
    }
}

#[derive(Eq, PartialEq)]
pub struct WireField {
    pub number: u8,
    pub developer_data_index: Option<u8>,
    pub base_type: Option<u8>,
    pub bytes: Vec<u8>,
}

#[derive(Eq, PartialEq)]
pub struct FitWireMessage {
    pub frame_index: u32,
    pub message_index: u32,
    pub byte_offset: u64,
    pub global_number: u16,
    pub local_number: u8,
    pub byte_order: ByteOrder,
    pub developer_data_flag: bool,
    pub compressed_timestamp: Option<u64>,
    pub fields: Vec<WireField>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct WireInventory {
    pub member: FitInventory,
    pub messages: u32,
}

#[derive(Clone, Copy)]
pub(crate) struct FieldDefinition {
    pub(crate) number: u8,
    size: u8,
    base_type: Option<u8>,
    pub(crate) developer_data_index: Option<u8>,
}

pub(crate) struct Definition {
    pub(crate) global_number: u16,
    order: ByteOrder,
    pub(crate) fields: Vec<FieldDefinition>,
}

pub(crate) enum WireRecord<'a> {
    Frame,
    Definition(u8, &'a Definition),
    Message(&'a FitWireMessage),
}

struct Body<'a, R> {
    source: &'a mut R,
    remaining: u32,
    checksum: Digest<'static, u16>,
}

impl<R: Read> Body<'_, R> {
    fn read(&mut self, bytes: &mut [u8]) -> Result<(), FitError> {
        let size = bytes.len() as u32;
        if size > self.remaining {
            return Err(FitError::InvalidRecord);
        }
        self.source.read_exact(bytes)?;
        self.checksum.update(bytes);
        self.remaining -= size;
        Ok(())
    }
}

/// Read the bytes behind each FIT message without a profile-based type substitution.
/// Results are candidates until all sequence checksums and the member have passed.
pub fn visit_fit_wire(
    source: impl Read,
    max_bytes: u64,
    cancelled: impl FnMut() -> bool,
    mut visit: impl FnMut(&FitWireMessage) -> Result<(), FitError>,
) -> Result<WireInventory, FitError> {
    visit_fit_records(source, max_bytes, cancelled, |record| match record {
        WireRecord::Message(message) => visit(message),
        _ => Ok(()),
    })
}

pub(crate) fn visit_fit_records(
    mut source: impl Read,
    max_bytes: u64,
    mut cancelled: impl FnMut() -> bool,
    mut visit: impl FnMut(WireRecord<'_>) -> Result<(), FitError>,
) -> Result<WireInventory, FitError> {
    let mut inventory = WireInventory::default();
    let limit = max_bytes.min(MAX_FIT_BYTES);
    loop {
        check_cancelled(&mut cancelled)?;
        let Some((frame, header)) = read_frame_header(&mut source, inventory.member, limit)? else {
            return Ok(inventory);
        };
        visit(WireRecord::Frame)?;
        let mut checksum = FIT_CRC.digest();
        checksum.update(&header[..usize::from(frame.header_bytes)]);
        let mut body = Body {
            source: &mut source,
            remaining: frame.data_bytes,
            checksum,
        };
        let mut definitions: [Option<Definition>; 16] = std::array::from_fn(|_| None);
        let mut timestamp = 0_u64;
        let mut fields = Vec::new();
        while body.remaining != 0 {
            check_cancelled(&mut cancelled)?;
            let offset = frame.offset
                + u64::from(frame.header_bytes)
                + u64::from(frame.data_bytes - body.remaining);
            let mut header = [0; 1];
            body.read(&mut header)?;
            let header = header[0];
            let compressed = header & 0x80 != 0;
            let local = if compressed {
                (header >> 5) & 0x03
            } else {
                header & 0x0f
            };
            if !compressed && header & 0x40 != 0 {
                let definition = read_definition(&mut body, header & 0x20 != 0)?;
                visit(WireRecord::Definition(local, &definition))?;
                definitions[usize::from(local)] = Some(definition);
                continue;
            }
            let definition = definitions[usize::from(local)]
                .as_ref()
                .ok_or(FitError::MissingDefinition)?;
            fields.truncate(definition.fields.len());
            for (index, field) in definition.fields.iter().enumerate() {
                if field.size == 0 {
                    return Err(FitError::InvalidDefinition);
                }
                if index == fields.len() {
                    fields.push(WireField {
                        number: field.number,
                        developer_data_index: field.developer_data_index,
                        base_type: field.base_type,
                        bytes: Vec::new(),
                    });
                }
                let value = &mut fields[index];
                value.number = field.number;
                value.developer_data_index = field.developer_data_index;
                value.base_type = field.base_type;
                value.bytes.resize(usize::from(field.size), 0);
                body.read(&mut value.bytes)?;
                if value.number == 253
                    && value.base_type == Some(0x86)
                    && let Ok(bytes) = value.bytes.as_slice().try_into()
                {
                    let recorded = definition.order.u32(bytes);
                    if recorded != u32::MAX {
                        timestamp = u64::from(recorded);
                    }
                }
            }
            let compressed_timestamp = compressed.then(|| {
                let offset = u64::from(header & 0x1f);
                let advance = (offset + 32 - (timestamp & 0x1f)) & 0x1f;
                timestamp += advance;
                timestamp
            });
            let message = FitWireMessage {
                frame_index: frame.index,
                message_index: inventory.messages,
                byte_offset: offset,
                global_number: definition.global_number,
                local_number: local,
                byte_order: definition.order,
                developer_data_flag: !compressed && header & 0x20 != 0,
                compressed_timestamp,
                fields,
            };
            visit(WireRecord::Message(&message))?;
            fields = message.fields;
            inventory.messages += 1;
        }
        check_cancelled(&mut cancelled)?;
        verify_checksum(body.source, body.checksum.finalize())?;
        inventory.member.frames += 1;
        inventory.member.bytes = frame_end(frame);
    }
}

fn read_definition(
    body: &mut Body<'_, impl Read>,
    with_developers: bool,
) -> Result<Definition, FitError> {
    let mut fixed = [0; 5];
    body.read(&mut fixed)?;
    let order = if fixed[1] == 0 {
        ByteOrder::Little
    } else {
        ByteOrder::Big
    };
    let mut fields = Vec::with_capacity(usize::from(fixed[4]));
    for _ in 0..fixed[4] {
        let [number, size, base_type] = definition_bytes(body)?;
        fields.push(FieldDefinition {
            number,
            size,
            base_type: Some(base_type),
            developer_data_index: None,
        });
    }
    if with_developers {
        let mut count = [0; 1];
        body.read(&mut count)?;
        fields.reserve(usize::from(count[0]));
        for _ in 0..count[0] {
            let [number, size, index] = definition_bytes(body)?;
            fields.push(FieldDefinition {
                number,
                size,
                base_type: None,
                developer_data_index: Some(index),
            });
        }
    }
    Ok(Definition {
        global_number: order.u16([fixed[2], fixed[3]]),
        order,
        fields,
    })
}

fn definition_bytes(body: &mut Body<'_, impl Read>) -> Result<[u8; 3], FitError> {
    let mut bytes = [0; 3];
    body.read(&mut bytes)?;
    Ok(bytes)
}
