pub mod event_contract;
pub mod event_input;
pub mod event_payload;
pub mod event_provenance;
pub mod event_replay;
pub mod event_schema;
mod event_time;
pub mod garmin_health;
pub mod garmin_summary;
pub mod identity;
pub mod language_tags;
pub mod legacy_json;
pub mod source_contract;
pub mod statistics;
pub mod text_offsets;
pub mod timestamp;

use sha2::{Digest, Sha256};

pub fn sha256_hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut hex = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        hex.push(char::from(DIGITS[usize::from(byte >> 4)]));
        hex.push(char::from(DIGITS[usize::from(byte & 0x0f)]));
    }
    hex
}

pub mod metric_contract;
