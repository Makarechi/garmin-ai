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
    pub fn resolve(
        integrations: Option<&str>,
        token_directory: &Path,
    ) -> Result<Option<Self>, GarminConfigurationError> {
        let primary = || Self {
            instance: LEGACY_SOURCE.into(),
            token_directory: token_directory.to_owned(),
        };
        let Some(config) = integrations else {
            let saved = GarminTokenFile::open_existing(token_directory)
                .map_err(GarminConfigurationError::TokenDirectory)?;
            return Ok(saved.map(|_| primary()));
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

/// Validate the administrator-configured URL for the Garmin authentication page.
pub fn garmin_auth_url(value: &str) -> Option<reqwest::Url> {
    let url = reqwest::Url::parse(value).ok()?;
    (url.scheme() == "https"
        && url.host().is_some()
        && url.path() == "/garmin-auth"
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none())
    .then_some(url)
}

pub struct GarminReauthNoticeSettings {
    pub(crate) source: String,
    pub(crate) launch: Option<reqwest::Url>,
}

impl GarminReauthNoticeSettings {
    pub fn resolve(
        integrations: Option<&str>,
        token_directory: &Path,
        launch: Option<&str>,
        email: Option<&str>,
        password_secret: Option<String>,
    ) -> Result<Option<Self>, GarminConfigurationError> {
        let Some(source) = garmin_auth_source(integrations, token_directory)? else {
            return Ok(None);
        };
        let launch = launch.and_then(garmin_auth_url).filter(|_| {
            email.is_some_and(|value| !value.is_empty())
                && password_secret.is_some_and(|value| {
                    ga_garmin::managed_password::GarminPasswordSecret::pinned(value).is_ok()
                })
        });
        Ok(Some(Self { source, launch }))
    }
}

pub fn garmin_auth_source(
    integrations: Option<&str>,
    token_directory: &Path,
) -> Result<Option<String>, GarminConfigurationError> {
    if integrations.is_none() {
        return Ok(Some(LEGACY_SOURCE.to_owned()));
    }
    Ok(GarminSourceSettings::resolve(integrations, token_directory)?.map(|source| source.instance))
}
