//! Resolve Telegram ingress from selected instances and existing owner pairings.

use std::{error::Error, fmt};

use ga_db::{Database, telegram_binding::TelegramBindingError};
use serde_json::Value;

use crate::telegram_input::TelegramBinding;

#[derive(Debug, PartialEq, Eq)]
pub struct TelegramIngressSettings {
    instance_id: String,
    external_owner_id: i64,
}

#[derive(Debug)]
pub enum TelegramSettingsError {
    InvalidOwner,
    InvalidIntegrations,
    Pairing(TelegramBindingError),
}

impl fmt::Display for TelegramSettingsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidOwner => f.write_str("Configured Telegram owner is invalid"),
            Self::InvalidIntegrations => f.write_str("Configured Telegram integration is invalid"),
            Self::Pairing(error) => error.fmt(f),
        }
    }
}
impl Error for TelegramSettingsError {}

impl TelegramIngressSettings {
    pub fn selected(
        telegram_user_id: Option<&str>,
        integrations: Option<&str>,
    ) -> Result<Option<Self>, TelegramSettingsError> {
        let external_owner_id = telegram_user_id
            .map_or(Ok(0), |value| value.trim().parse::<i64>())
            .map_err(|_| TelegramSettingsError::InvalidOwner)?;
        if !(0..(1_i64 << 52)).contains(&external_owner_id) {
            return Err(TelegramSettingsError::InvalidOwner);
        }

        let configured: Vec<Value> = integrations
            .map(serde_json::from_str)
            .transpose()
            .map_err(|_| TelegramSettingsError::InvalidIntegrations)?
            .unwrap_or_default();
        if configured.len() > 32 {
            return Err(TelegramSettingsError::InvalidIntegrations);
        }
        if external_owner_id == 0 {
            return Ok(None);
        }
        let mut instance_id = None;
        for item in &configured {
            if item["kind"] != "channel" || item["provider"] != "telegram" {
                continue;
            }
            let enabled =
                enabled(item.get("enabled")).ok_or(TelegramSettingsError::InvalidIntegrations)?;
            if !enabled {
                continue;
            }
            let configured_id = item["id"]
                .as_str()
                .filter(|id| valid_instance_id(id))
                .ok_or(TelegramSettingsError::InvalidIntegrations)?;
            let namespace = configured_id
                .strip_prefix("channel:telegram:")
                .unwrap_or(configured_id);
            if namespace.is_empty() {
                return Err(TelegramSettingsError::InvalidIntegrations);
            }
            instance_id = Some(namespace.to_owned());
            break;
        }
        if configured.is_empty() {
            instance_id = Some("primary".to_owned());
        }
        Ok(instance_id.map(|instance_id| Self {
            instance_id,
            external_owner_id,
        }))
    }

    pub fn instance_id(&self) -> &str {
        &self.instance_id
    }

    pub fn external_owner_id(&self) -> i64 {
        self.external_owner_id
    }

    pub async fn confirmed_binding<'a>(
        &'a self,
        database: &Database,
    ) -> Result<TelegramBinding<'a>, TelegramSettingsError> {
        let owner_id = database
            .confirmed_telegram_owner(&self.instance_id, self.external_owner_id)
            .await
            .map_err(TelegramSettingsError::Pairing)?;
        Ok(TelegramBinding {
            owner_id,
            instance_id: &self.instance_id,
            external_owner_id: self.external_owner_id,
        })
    }
}

fn valid_instance_id(id: &str) -> bool {
    (1..=200).contains(&id.len())
        && id.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
        && id.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.:-".contains(&byte)
        })
}

fn enabled(value: Option<&Value>) -> Option<bool> {
    match value {
        None => Some(true),
        Some(Value::Bool(enabled)) => Some(*enabled),
        Some(Value::Number(number)) => match number.as_f64()? {
            0.0 => Some(false),
            1.0 => Some(true),
            _ => None,
        },
        Some(Value::String(value)) => match value.to_ascii_lowercase().as_str() {
            "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
            "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
            _ => None,
        },
        _ => None,
    }
}
