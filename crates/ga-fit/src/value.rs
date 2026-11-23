use crate::{ByteOrder, FitError, WireField};

/// A wire value before profile enumeration, units, scaling or component expansion.
#[derive(Clone, PartialEq)]
pub enum RawFitValue {
    Invalid,
    Unsigned(u64),
    Signed(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Array(Vec<Self>),
}

impl WireField {
    /// Native types come from the FIT definition. A developer field needs its descriptor.
    pub fn decode_native(
        &self,
        order: ByteOrder,
    ) -> Result<Option<(&'static str, RawFitValue)>, FitError> {
        self.base_type
            .map(|identifier| decode(identifier, order, &self.bytes))
            .transpose()
    }
}

pub(crate) fn decode(
    identifier: u8,
    order: ByteOrder,
    data: &[u8],
) -> Result<(&'static str, RawFitValue), FitError> {
    use RawFitValue::{Array, Bytes, Float, Invalid, Signed, Text, Unsigned};

    if data.is_empty() {
        return Err(FitError::InvalidDefinition);
    }
    let (name, width) = match identifier {
        0x00 => ("enum", 1),
        0x01 => ("sint8", 1),
        0x02 => ("uint8", 1),
        0x83 => ("sint16", 2),
        0x84 => ("uint16", 2),
        0x85 => ("sint32", 4),
        0x86 => ("uint32", 4),
        0x07 => ("string", 1),
        0x88 => ("float32", 4),
        0x89 => ("float64", 8),
        0x0a => ("uint8z", 1),
        0x8b => ("uint16z", 2),
        0x8c => ("uint32z", 4),
        0x8e => ("sint64", 8),
        0x8f => ("uint64", 8),
        0x90 => ("uint64z", 8),
        _ => ("byte", 1),
    };
    if name == "byte" || !data.len().is_multiple_of(width) {
        let value = if data.iter().all(|byte| *byte == 255) {
            Invalid
        } else {
            Bytes(data.to_vec())
        };
        return Ok(("byte", value));
    }
    if identifier == 0x07 {
        let end = data
            .iter()
            .position(|byte| *byte == 0)
            .unwrap_or(data.len());
        let value = if end == 0 {
            Invalid
        } else {
            Text(String::from_utf8_lossy(&data[..end]).into_owned())
        };
        return Ok((name, value));
    }
    let read = |bytes: &[u8]| {
        let mut word = [0_u8; 8];
        let bits = match order {
            ByteOrder::Little => {
                word[..width].copy_from_slice(bytes);
                u64::from_le_bytes(word)
            }
            ByteOrder::Big => {
                word[8 - width..].copy_from_slice(bytes);
                u64::from_be_bytes(word)
            }
        };
        let sign_shift = (8 - width) * 8;
        match identifier {
            0x01 | 0x83 | 0x85 | 0x8e => {
                let signed = (bits << sign_shift).cast_signed() >> sign_shift;
                if signed == i64::MAX >> sign_shift {
                    Invalid
                } else {
                    Signed(signed)
                }
            }
            0x88 | 0x89 => {
                let value = if identifier == 0x88 {
                    let Ok(bits) = u32::try_from(bits) else {
                        return Invalid;
                    };
                    f64::from(f32::from_bits(bits))
                } else {
                    f64::from_bits(bits)
                };
                if value.is_nan() {
                    Invalid
                } else {
                    Float(value)
                }
            }
            0x0a | 0x8b | 0x8c | 0x90 => {
                if bits == 0 {
                    Invalid
                } else {
                    Unsigned(bits)
                }
            }
            _ => {
                if bits == u64::MAX >> sign_shift {
                    Invalid
                } else {
                    Unsigned(bits)
                }
            }
        }
    };
    if data.len() == width {
        Ok((name, read(data)))
    } else {
        Ok((name, Array(data.chunks_exact(width).map(read).collect())))
    }
}
