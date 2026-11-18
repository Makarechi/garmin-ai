use std::{error::Error, fmt, time::Duration};

use base64::{Engine, engine::general_purpose::STANDARD};
use reqwest::{Client, Response};
use serde::{Deserialize, de::DeserializeOwned};
use zeroize::Zeroizing;

use crate::{TransportConfig, build_http, read_bounded, sensitive_header};

const METADATA_TOKEN: &str =
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token";
const SECRET_MANAGER: &str = "https://secretmanager.googleapis.com/v1";
const METADATA_TIMEOUT: Duration = Duration::from_secs(5);
const SECRET_TIMEOUT: Duration = Duration::from_secs(10);
const RESPONSE_LIMIT: u64 = 96 * 1024;

pub struct GarminPasswordSecret(String);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct GarminPasswordUnavailable;

impl fmt::Display for GarminPasswordUnavailable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("The configured Garmin password secret is unavailable")
    }
}
impl Error for GarminPasswordUnavailable {}

impl GarminPasswordSecret {
    pub fn pinned(version: String) -> Result<Self, GarminPasswordUnavailable> {
        if version.len() > 512 {
            return Err(GarminPasswordUnavailable);
        }
        let parts = version.split('/').collect::<Vec<_>>();
        let ["projects", project, "secrets", secret, "versions", number] = parts.as_slice() else {
            return Err(GarminPasswordUnavailable);
        };
        if !identifier(project)
            || !identifier(secret)
            || !number.starts_with(|part: char| ('1'..='9').contains(&part))
            || !number.bytes().all(|part| part.is_ascii_digit())
        {
            return Err(GarminPasswordUnavailable);
        }
        Ok(Self(version))
    }

    /// Fetch one configured version using the VM service identity.
    pub async fn read(&self) -> Result<Zeroizing<String>, GarminPasswordUnavailable> {
        let configuration = TransportConfig {
            connect_timeout: METADATA_TIMEOUT,
            read_timeout: SECRET_TIMEOUT,
            total_timeout: SECRET_TIMEOUT,
            ..TransportConfig::default()
        };
        let client = build_http(configuration, false)
            .and_then(|builder| {
                builder
                    .build()
                    .map_err(|_| crate::GarminError::InvalidConfiguration)
            })
            .map_err(|_| GarminPasswordUnavailable)?;
        self.read_from(&client, METADATA_TOKEN, SECRET_MANAGER)
            .await
    }

    async fn read_from(
        &self,
        client: &Client,
        metadata: &str,
        secrets: &str,
    ) -> Result<Zeroizing<String>, GarminPasswordUnavailable> {
        let identity: MetadataIdentity = decode(
            client
                .get(metadata)
                .header("Metadata-Flavor", "Google")
                .timeout(METADATA_TIMEOUT)
                .send()
                .await
                .map_err(|_| GarminPasswordUnavailable)?,
        )
        .await?;
        let token = Zeroizing::new(identity.access_token);
        if token.is_empty() {
            return Err(GarminPasswordUnavailable);
        }
        let authorization =
            sensitive_header("Bearer", &token).map_err(|_| GarminPasswordUnavailable)?;
        let secret: SecretVersion = decode(
            client
                .get(format!("{secrets}/{}:access", self.0))
                .header(reqwest::header::AUTHORIZATION, authorization)
                .timeout(SECRET_TIMEOUT)
                .send()
                .await
                .map_err(|_| GarminPasswordUnavailable)?,
        )
        .await?;
        let encoded = Zeroizing::new(secret.payload.data);
        let bytes = STANDARD
            .decode(encoded.as_bytes())
            .map_err(|_| GarminPasswordUnavailable)?;
        let value = String::from_utf8(bytes).map_err(|error| {
            drop(Zeroizing::new(error.into_bytes()));
            GarminPasswordUnavailable
        })?;
        if value.is_empty() {
            return Err(GarminPasswordUnavailable);
        }
        Ok(Zeroizing::new(value))
    }
}

fn identifier(part: &str) -> bool {
    !part.is_empty()
        && part
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
}

async fn decode<T: DeserializeOwned>(response: Response) -> Result<T, GarminPasswordUnavailable> {
    if !response.status().is_success() {
        return Err(GarminPasswordUnavailable);
    }
    let bytes = Zeroizing::new(
        read_bounded(response, RESPONSE_LIMIT)
            .await
            .map_err(|_| GarminPasswordUnavailable)?,
    );
    serde_json::from_slice(&bytes).map_err(|_| GarminPasswordUnavailable)
}

#[derive(Deserialize)]
struct MetadataIdentity {
    access_token: String,
}

#[derive(Deserialize)]
struct SecretVersion {
    payload: SecretPayload,
}

#[derive(Deserialize)]
struct SecretPayload {
    data: String,
}

#[cfg(test)]
mod tests;
