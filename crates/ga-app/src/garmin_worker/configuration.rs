use std::{
    error::Error,
    fmt,
    path::{Path, PathBuf},
};

use ga_archive::{ArchiveError, GarminTokenFile};
use ga_domain::event_payload::legacy_boolean;
use serde_json::Value;

const LEGACY_SOURCE: &str = "source:garmin:primary";

#[derive(Debug)]
pub enum GarminConfigurationError {
    Invalid,
    TokenDirectory(ArchiveError),
}

impl fmt::Display for GarminConfigurationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid => f.write_str("Garmin source configuration is invalid"),
            Self::TokenDirectory(_) => f.write_str("Garmin token directory is unavailable"),
        }
    }
}
impl Error for GarminConfigurationError {}

pub struct GarminSourceSettings {
    instance: String,
    token_directory: PathBuf,
}

impl GarminSourceSettings {
    fn primary(token_directory: &Path) -> Self {
        Self {
            instance: LEGACY_SOURCE.into(),
            token_directory: token_directory.to_owned(),
        }
    }

    /// The worker can discover a newly created legacy token file without restarting.
    pub fn for_worker(
        integrations: Option<&str>,
        token_directory: &Path,
    ) -> Result<Option<Self>, GarminConfigurationError> {
        if integrations.is_none() {
            Ok(Some(Self::primary(token_directory)))
        } else {
            Self::resolve(integrations, token_directory)
        }
    }

    pub fn resolve(
        integrations: Option<&str>,
        token_directory: &Path,
    ) -> Result<Option<Self>, GarminConfigurationError> {
        let Some(config) = integrations else {
            let saved = GarminTokenFile::open_existing(token_directory)
                .map_err(GarminConfigurationError::TokenDirectory)?;
            return Ok(saved.map(|_| Self::primary(token_directory)));
        };
        let integrations: Vec<Value> =
            serde_json::from_str(config).map_err(|_| GarminConfigurationError::Invalid)?;
        if integrations.len() > 32 {
            return Err(GarminConfigurationError::Invalid);
        }
        for integration in integrations {
            if integration["kind"] != "source" || integration["provider"] != "garmin" {
                continue;
            }
            let enabled = integration
                .get("enabled")
                .map_or(Some(true), legacy_boolean)
                .ok_or(GarminConfigurationError::Invalid)?;
            if enabled {
                let instance = integration["id"]
                    .as_str()
                    .filter(|value| valid_id(value))
                    .ok_or(GarminConfigurationError::Invalid)?;
                return Ok(Some(Self {
                    instance: instance.to_owned(),
                    token_directory: token_directory.to_owned(),
                }));
            }
        }
        Ok(None)
    }

    pub fn instance_id(&self) -> &str {
        &self.instance
    }

    pub fn existing_tokens(&self) -> Result<Option<GarminTokenFile>, GarminConfigurationError> {
        GarminTokenFile::open_existing(&self.token_directory)
            .map_err(GarminConfigurationError::TokenDirectory)
    }
}

fn valid_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 200
        && bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.:-".contains(&byte)
        })
}
