use std::{error::Error, fmt, num::NonZeroU64};

use jiter::JsonValue;
use uuid::Uuid;

use crate::{metric_contract::decimal_value, sha256_hex};

/// Parse the UUID spelling accepted by Python for legacy page cursors.
pub fn python_cursor_uuid(value: &str) -> Option<Uuid> {
    // Python strips these literals in order before validating the 32 code points.
    let unwrapped = value.replace("urn:", "").replace("uuid:", "");
    let mut digits = unwrapped
        .trim_matches(['{', '}'])
        .chars()
        .filter(|&c| c != '-');
    let mut ascii = [0u8; 32];
    for byte in &mut ascii {
        *byte = match digits.next()? {
            c if c.is_ascii() => c as u8,
            c if c.is_whitespace() => b' ',
            c => b'0' + decimal_value(c)?,
        };
    }
    if digits.next().is_some() {
        return None;
    }
    // UUID's constructor parses this normalized string as a base-16 Python integer.
    let text = str::from_utf8(&ascii)
        .ok()?
        .trim_matches(|c| matches!(c, '\t'..='\r' | ' '));
    let text = text.strip_prefix('+').unwrap_or(text);
    let digits = match text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
        Some(prefixed) => prefixed.strip_prefix('_').unwrap_or(prefixed),
        None => text,
    };
    let mut identity = 0u128;
    for group in digits.split('_') {
        if group.is_empty() {
            return None;
        }
        for digit in group.chars() {
            identity = identity * 16 + u128::from(digit.to_digit(16)?);
        }
    }
    Some(Uuid::from_u128(identity))
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub struct GarminProfileId(NonZeroU64);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProfileIdUnavailable;

impl fmt::Display for ProfileIdUnavailable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Authenticated stable profile identity unavailable")
    }
}

impl Error for ProfileIdUnavailable {}

impl GarminProfileId {
    pub fn from_profile_json(bytes: &[u8], max_bytes: usize) -> Result<Self, ProfileIdUnavailable> {
        if bytes.len() > max_bytes {
            return Err(ProfileIdUnavailable);
        }
        let JsonValue::Object(profile) =
            JsonValue::parse(bytes, true).map_err(|_| ProfileIdUnavailable)?
        else {
            return Err(ProfileIdUnavailable);
        };
        let identity = profile
            .iter()
            .rev()
            .find(|(key, _)| key == "profileId")
            .map(|(_, value)| value);
        match identity {
            Some(JsonValue::Int(value)) => {
                let positive = u64::try_from(*value).map_err(|_| ProfileIdUnavailable)?;
                NonZeroU64::new(positive)
                    .map(Self)
                    .ok_or(ProfileIdUnavailable)
            }
            Some(JsonValue::Str(value)) => value.parse(),
            _ => Err(ProfileIdUnavailable),
        }
    }

    pub fn fingerprint(self) -> String {
        sha256_hex(format!("garmin:socialProfile:profileId:v1:{}", self.0).as_bytes())
    }
}

impl std::str::FromStr for GarminProfileId {
    type Err = ProfileIdUnavailable;

    fn from_str(identity: &str) -> Result<Self, Self::Err> {
        if identity.len() > 19 || !identity.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(ProfileIdUnavailable);
        }
        let value: NonZeroU64 = identity.parse().map_err(|_| ProfileIdUnavailable)?;
        if value.get() > i64::MAX as u64 {
            return Err(ProfileIdUnavailable);
        }
        Ok(Self(value))
    }
}
