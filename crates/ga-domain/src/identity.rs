use std::{error::Error, fmt, num::NonZeroU64};

use jiter::JsonValue;

use crate::sha256_hex;

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
