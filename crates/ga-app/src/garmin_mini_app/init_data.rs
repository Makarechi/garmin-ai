use std::{collections::BTreeMap, error::Error, fmt};

use hmac::{Hmac, KeyInit, Mac};
use serde_json::Value;
use sha2::{Digest, Sha256};

const MAX_INIT_DATA_BYTES: usize = 8192;
const MAX_INITIAL_AGE_SECONDS: i64 = 300;
const ALLOWED_CLOCK_LEAD_SECONDS: i64 = 30;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum TelegramSessionStage {
    Start,
    Complete,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct InvalidTelegramSession;

impl fmt::Display for InvalidTelegramSession {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Telegram Mini App session is invalid or expired")
    }
}

impl Error for InvalidTelegramSession {}

/// Authenticate the bot owner and return an opaque value for binding an MFA challenge.
pub(super) fn validate_init_data(
    raw: &str,
    bot_token: &str,
    owner: i64,
    now: i64,
    stage: TelegramSessionStage,
) -> Result<[u8; 32], InvalidTelegramSession> {
    if raw.is_empty()
        || raw.len() > MAX_INIT_DATA_BYTES
        || bot_token.is_empty()
        || !(1..(1_i64 << 52)).contains(&owner)
        || raw.split('&').any(|part| !part.contains('='))
    {
        return Err(InvalidTelegramSession);
    }
    let mut fields = BTreeMap::new();
    for (key, value) in form_urlencoded::parse(raw.as_bytes()) {
        if fields.insert(key, value).is_some() {
            return Err(InvalidTelegramSession);
        }
    }
    let signature = fields.remove("hash").ok_or(InvalidTelegramSession)?;
    let signature = digest_bytes(signature.as_bytes()).ok_or(InvalidTelegramSession)?;
    let mut bot =
        Hmac::<Sha256>::new_from_slice(b"WebAppData").map_err(|_| InvalidTelegramSession)?;
    bot.update(bot_token.as_bytes());
    let derived = bot.finalize().into_bytes();
    let mut verifier =
        Hmac::<Sha256>::new_from_slice(&derived).map_err(|_| InvalidTelegramSession)?;
    for (index, (key, value)) in fields.iter().enumerate() {
        if index > 0 {
            verifier.update(b"\n");
        }
        verifier.update(key.as_bytes());
        verifier.update(b"=");
        verifier.update(value.as_bytes());
    }
    verifier
        .verify_slice(&signature)
        .map_err(|_| InvalidTelegramSession)?;
    let auth_at: i64 = fields
        .get("auth_date")
        .and_then(|value| value.trim().parse().ok())
        .ok_or(InvalidTelegramSession)?;
    let user: Value = fields
        .get("user")
        .and_then(|value| serde_json::from_str(value).ok())
        .ok_or(InvalidTelegramSession)?;
    if user["id"].as_i64() != Some(owner)
        || auth_at > now.saturating_add(ALLOWED_CLOCK_LEAD_SECONDS)
        || (stage == TelegramSessionStage::Start
            && now.saturating_sub(auth_at) > MAX_INITIAL_AGE_SECONDS)
    {
        return Err(InvalidTelegramSession);
    }
    Ok(Sha256::digest(raw.as_bytes()).into())
}

fn digest_bytes(value: &[u8]) -> Option<[u8; 32]> {
    fn hex(byte: u8) -> Option<u8> {
        match byte {
            b'0'..=b'9' => Some(byte - b'0'),
            b'a'..=b'f' => Some(byte - b'a' + 10),
            _ => None,
        }
    }
    let pairs = value.as_chunks::<2>();
    if pairs.0.len() != 32 || !pairs.1.is_empty() {
        return None;
    }
    let mut bytes = [0; 32];
    for (output, [high, low]) in bytes.iter_mut().zip(pairs.0) {
        *output = hex(*high)? << 4 | hex(*low)?;
    }
    Some(bytes)
}
