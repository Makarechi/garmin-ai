use std::{env, io};

use axum::http::HeaderValue;
use ga_domain::access::{Scope, Scopes};
use serde_json::Value;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use uuid::Uuid;

const MAX_SCOPED_TOKENS: usize = 32;
const SCOPED_TOKEN_FIELDS: &[&str] = &["key", "scopes", "wearable_device_id"];

#[derive(Clone, Default)]
pub(crate) struct Authorization {
    tokens: Vec<Token>,
}

#[derive(Clone)]
struct Token {
    digest: [u8; 32],
    scopes: Scopes,
}

impl Authorization {
    pub(crate) fn configured() -> io::Result<Self> {
        let [file_legacy, file_scoped] = read_settings_file()?;
        let legacy = env_value("GA_API_KEY")?.or(file_legacy);
        let scoped = env_value("GA_API_TOKENS")?.or(file_scoped);
        Self::parse(legacy.as_deref(), scoped.as_deref()).ok_or_else(invalid_credentials)
    }

    fn parse(legacy: Option<&str>, scoped: Option<&str>) -> Option<Self> {
        let mut tokens = Vec::new();
        if let Some(key) = legacy.filter(|key| strong(key)) {
            tokens.push(Token {
                digest: credential(key),
                scopes: [Scope::Admin].into_iter().collect(),
            });
        }
        if let Some(scoped) = scoped {
            let config: Value = serde_json::from_str(scoped).ok()?;
            let list = config
                .as_array()
                .filter(|list| list.len() <= MAX_SCOPED_TOKENS)?;
            for config in list {
                let token = scoped_token(config)?;
                if tokens
                    .iter()
                    .any(|existing| existing.digest == token.digest)
                {
                    return None;
                }
                tokens.push(token);
            }
        }
        Some(Self { tokens })
    }

    pub(crate) fn authenticate(&self, authorization: Option<&HeaderValue>) -> Option<Scopes> {
        let authorization = authorization?.as_bytes();
        // ASGI decodes header bytes as Latin-1. Preserve that mapping for old keys.
        let digest: [u8; 32] = if authorization.is_ascii() {
            Sha256::digest(authorization).into()
        } else {
            let value: String = authorization.iter().map(|byte| char::from(*byte)).collect();
            Sha256::digest(value.as_bytes()).into()
        };
        let mut granted = None;
        for token in &self.tokens {
            if bool::from(token.digest.ct_eq(&digest)) {
                granted = Some(token.scopes);
            }
        }
        granted
    }
}

fn scoped_token(value: &Value) -> Option<Token> {
    let fields = value.as_object()?;
    if fields
        .keys()
        .any(|field| !SCOPED_TOKEN_FIELDS.contains(&field.as_str()))
    {
        return None;
    }
    let key = fields.get("key")?.as_str().filter(|key| strong(key))?;
    let scopes: Scopes = match fields.get("scopes") {
        None => [Scope::ReadHealth].into_iter().collect(),
        Some(scopes) => scopes
            .as_array()?
            .iter()
            .map(|scope| Scope::from_name(scope.as_str()?))
            .collect::<Option<Scopes>>()?,
    };
    let device = match fields.get("wearable_device_id") {
        None | Some(Value::Null) => None,
        Some(device) => Some(Uuid::parse_str(device.as_str()?).ok()?),
    };
    let only_wearable: Scopes = [Scope::WriteWearable].into_iter().collect();
    if (scopes.includes(Scope::WriteWearable) && scopes != only_wearable)
        || (scopes == only_wearable) != device.is_some()
    {
        return None;
    }
    Some(Token {
        digest: credential(key),
        scopes,
    })
}

fn strong(key: &str) -> bool {
    key.chars().count() >= 32 && !key.starts_with("replace-with-")
}

fn credential(key: &str) -> [u8; 32] {
    let mut digest = Sha256::new();
    digest.update(b"Bearer ");
    digest.update(key.as_bytes());
    digest.finalize().into()
}

fn read_settings_file() -> io::Result<[Option<String>; 2]> {
    let mut result = [None, None];
    let entries = match dotenvy::from_path_iter(".env") {
        Ok(entries) => entries,
        Err(dotenvy::Error::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(result);
        }
        Err(_) => return Err(invalid_credentials()),
    };
    for (name, value) in entries.filter_map(Result::ok) {
        if name.eq_ignore_ascii_case("GA_API_KEY") {
            result[0] = Some(value);
        } else if name.eq_ignore_ascii_case("GA_API_TOKENS") {
            result[1] = Some(value);
        }
    }
    Ok(result)
}

fn env_value(name: &str) -> io::Result<Option<String>> {
    match env::var(name) {
        Ok(value) => Ok(Some(value)),
        Err(env::VarError::NotPresent) => Ok(None),
        Err(env::VarError::NotUnicode(_)) => Err(invalid_credentials()),
    }
}

fn invalid_credentials() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, "API credentials are invalid")
}
