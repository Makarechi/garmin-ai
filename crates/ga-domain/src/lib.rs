pub mod access;
pub mod activity_scan;
pub mod agent_interpretation;
pub mod analysis_budget;
pub mod channel;
pub mod event_contract;
pub mod event_input;
pub mod event_payload;
pub mod event_provenance;
pub mod event_replay;
pub mod event_schema;
mod event_time;
pub mod file_import;
pub mod garmin_health;
pub mod garmin_summary;
pub mod identity;
pub mod language_tags;
pub mod legacy_json;
pub mod legacy_metrics;
pub mod model_consent;
pub mod model_gate;
pub mod model_request;
pub mod model_response;
pub mod model_schema;
pub mod source_contract;
pub mod statistics;
pub mod telegram_control;
pub mod text_offsets;
pub mod timestamp;

use sha2::{Digest, Sha256};

pub fn sha256_hex(bytes: &[u8]) -> String {
    lower_hex(&Sha256::digest(bytes))
}

pub(crate) fn lower_hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut hex = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        hex.push(char::from(DIGITS[usize::from(byte >> 4)]));
        hex.push(char::from(DIGITS[usize::from(byte & 0x0f)]));
    }
    hex
}

pub mod analysis_claims;
pub mod metric_contract;
