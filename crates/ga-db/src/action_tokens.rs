use std::{error::Error, fmt};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ga_domain::{sha256_hex, timestamp::StoredInstant};
use hmac::{Hmac, KeyInit, Mac};
use serde::Deserialize;
use serde_json::json;
use sha2::Sha256;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;
use zeroize::Zeroizing;

use crate::{ReadinessError, check_readiness};

const NONCE_BYTES: usize = 18;
const SIGNATURE_BYTES: usize = 24;
const SIGNING_BYTES: usize = 32;
const TOKEN_LENGTH: usize = 57;

#[derive(Clone, Copy)]
pub struct ActionTokenContext {
    pub owner_id: Uuid,
    pub conversation_id: Uuid,
    pub revision: i32,
}

#[derive(Debug)]
pub enum ActionTokenError {
    InvalidAction,
    MissingOwner,
    SigningKey,
    Entropy,
    Storage(ReadinessError),
    Database,
}

impl fmt::Display for ActionTokenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidAction => "Action identifier, revision or expiration is invalid",
            Self::MissingOwner => "Action token owner is unavailable",
            Self::SigningKey => "Action token signing key is invalid",
            Self::Entropy => "Action token entropy is unavailable",
            Self::Storage(_) => "Action token storage is unavailable",
            Self::Database => "Action token state could not be accessed",
        })
    }
}
impl Error for ActionTokenError {}
impl From<sqlx::Error> for ActionTokenError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

/// Issue in the same transaction as the outbox intent using its verified owner and revision.
pub async fn issue_action_token(
    txn: &mut Transaction<'_, Postgres>,
    context: ActionTokenContext,
    action_id: &str,
    now: StoredInstant,
    expires_at: StoredInstant,
) -> Result<String, ActionTokenError> {
    if context.revision <= 0 || !valid_action_id(action_id) || expires_at <= now {
        return Err(ActionTokenError::InvalidAction);
    }
    check_readiness(txn)
        .await
        .map_err(ActionTokenError::Storage)?;
    let key = owner_key(txn, context.owner_id, true)
        .await?
        .ok_or(ActionTokenError::MissingOwner)?;
    let mut nonce = [0; NONCE_BYTES];
    getrandom::fill(&mut nonce).map_err(|_| ActionTokenError::Entropy)?;
    let encoded = URL_SAFE_NO_PAD.encode(nonce);
    let signature = mac(&key, &encoded)?.finalize().into_bytes();
    let token = format!(
        "{encoded}.{}",
        URL_SAFE_NO_PAD.encode(&signature[..SIGNATURE_BYTES])
    );
    sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
        .bind(token_key(&token))
        .bind(
            json!({
                "owner_id": context.owner_id.to_string(),
                "conversation_id": context.conversation_id.to_string(),
                "action_id": action_id,
                "revision": context.revision,
                "expires_at": expires_at.to_string(),
            })
            .to_string(),
        )
        .execute(&mut **txn)
        .await?;
    Ok(token)
}

/// Consume using the authenticated owner, bound conversation and current action revision.
/// The caller commits consumption and the matching action together or rolls both back.
pub async fn consume_action_token(
    txn: &mut Transaction<'_, Postgres>,
    context: ActionTokenContext,
    token: &str,
    now: StoredInstant,
) -> Result<Option<String>, ActionTokenError> {
    let Some((encoded, signature)) = token_parts(token) else {
        return Ok(None);
    };
    check_readiness(txn)
        .await
        .map_err(ActionTokenError::Storage)?;
    let Some(key) = owner_key(txn, context.owner_id, false).await? else {
        return Ok(None);
    };
    if mac(&key, encoded)?
        .verify_truncated_left(&signature)
        .is_err()
    {
        return Ok(None);
    }
    let identity = token_key(token);
    let stored: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1 FOR UPDATE")
            .bind(&identity)
            .fetch_optional(&mut **txn)
            .await?;
    let Some(action) = stored
        .as_deref()
        .and_then(|value| serde_json::from_str::<RecordedAction>(value).ok())
    else {
        return Ok(None);
    };
    if action.owner_id != context.owner_id.to_string()
        || action.conversation_id != context.conversation_id.to_string()
        || action.revision != context.revision
        || action
            .expires_at
            .parse::<StoredInstant>()
            .ok()
            .is_none_or(|expires_at| expires_at <= now)
        || !valid_action_id(&action.action_id)
    {
        return Ok(None);
    }
    sqlx::query("DELETE FROM app_state WHERE key=$1")
        .bind(&identity)
        .execute(&mut **txn)
        .await?;
    Ok(Some(action.action_id))
}

fn valid_action_id(value: &str) -> bool {
    (1..=200).contains(&value.chars().count())
}

#[derive(Deserialize)]
struct RecordedAction {
    owner_id: String,
    conversation_id: String,
    action_id: String,
    revision: i32,
    expires_at: String,
}

#[derive(Deserialize)]
struct RecordedKey {
    key: String,
}

async fn owner_key(
    txn: &mut Transaction<'_, Postgres>,
    owner_id: Uuid,
    create: bool,
) -> Result<Option<Zeroizing<Vec<u8>>>, ActionTokenError> {
    // Python holds the owner row lock before reading or creating this signing key.
    let owner: Option<Uuid> = sqlx::query_scalar("SELECT id FROM people WHERE id=$1 FOR UPDATE")
        .bind(owner_id)
        .fetch_optional(&mut **txn)
        .await?;
    if owner.is_none() {
        return Ok(None);
    }

    let identity = format!("action-signing-key:{owner_id}");
    let stored: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(&identity)
            .fetch_optional(&mut **txn)
            .await?;
    if let Some(stored) = stored {
        let stored = Zeroizing::new(stored);
        let record: RecordedKey =
            serde_json::from_str(&stored).map_err(|_| ActionTokenError::SigningKey)?;
        let encoded = Zeroizing::new(record.key);
        let key = Zeroizing::new(
            URL_SAFE_NO_PAD
                .decode(encoded.as_bytes())
                .map_err(|_| ActionTokenError::SigningKey)?,
        );
        if key.len() < SIGNING_BYTES {
            return Err(ActionTokenError::SigningKey);
        }
        return Ok(Some(key));
    }
    if !create {
        return Ok(None);
    }

    let mut key = Zeroizing::new(vec![0; SIGNING_BYTES]);
    getrandom::fill(&mut key).map_err(|_| ActionTokenError::Entropy)?;
    let stored = Zeroizing::new(json!({"key": URL_SAFE_NO_PAD.encode(key.as_slice())}).to_string());
    sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
        .bind(identity)
        .bind(stored.as_str())
        .execute(&mut **txn)
        .await?;
    Ok(Some(key))
}

fn mac(key: &[u8], encoded: &str) -> Result<Hmac<Sha256>, ActionTokenError> {
    let mut mac =
        <Hmac<Sha256> as KeyInit>::new_from_slice(key).map_err(|_| ActionTokenError::SigningKey)?;
    mac.update(encoded.as_bytes());
    Ok(mac)
}

fn token_parts(token: &str) -> Option<(&str, [u8; SIGNATURE_BYTES])> {
    if token.len() != TOKEN_LENGTH || !token.is_ascii() {
        return None;
    }
    let (nonce, tag) = token.split_once('.')?;
    let mut decoded_nonce = [0; NONCE_BYTES];
    let mut signature = [0; SIGNATURE_BYTES];
    if URL_SAFE_NO_PAD
        .decode_slice(nonce, &mut decoded_nonce)
        .ok()?
        != NONCE_BYTES
        || URL_SAFE_NO_PAD.decode_slice(tag, &mut signature).ok()? != SIGNATURE_BYTES
    {
        return None;
    }
    Some((nonce, signature))
}

fn token_key(token: &str) -> String {
    format!("action-token:{}", sha256_hex(token.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::{NONCE_BYTES, SIGNATURE_BYTES, TOKEN_LENGTH, token_parts};
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};

    #[test]
    fn accepts_the_full_range_of_encoded_nonce_and_signature_bytes() {
        for byte in u8::MIN..=u8::MAX {
            let nonce = URL_SAFE_NO_PAD.encode([byte; NONCE_BYTES]);
            let signature = [!byte; SIGNATURE_BYTES];
            let token = format!("{nonce}.{}", URL_SAFE_NO_PAD.encode(signature));
            assert_eq!(token.len(), TOKEN_LENGTH);
            assert_eq!(token_parts(&token), Some((nonce.as_str(), signature)));
        }
    }

    #[test]
    fn accepts_only_the_url_safe_alphabet_and_one_delimiter_at_fixed_offsets()
    -> Result<(), std::str::Utf8Error> {
        let nonce = URL_SAFE_NO_PAD.encode([0; NONCE_BYTES]);
        let signed = URL_SAFE_NO_PAD.encode([0; SIGNATURE_BYTES]);
        let valid = format!("{nonce}.{signed}");
        let delimiter = nonce.len();
        let mut bytes = valid.as_bytes().to_vec();
        for (index, original) in valid.bytes().enumerate() {
            for replacement in 0..=127 {
                bytes[index] = replacement;
                let text = str::from_utf8(&bytes)?;
                let expected = if index == delimiter {
                    replacement == b'.'
                } else {
                    replacement.is_ascii_alphanumeric() || matches!(replacement, b'-' | b'_')
                };
                assert_eq!(
                    token_parts(text).is_some(),
                    expected,
                    "{index}, {replacement}"
                );
            }
            bytes[index] = original;
        }

        for replacement in ["é", "\u{a0}", "中", "🧪"] {
            let mut unicode = valid.clone();
            unicode.replace_range(..replacement.len(), replacement);
            assert_eq!(unicode.len(), TOKEN_LENGTH);
            assert!(token_parts(&unicode).is_none());
        }
        for removed in 1..=TOKEN_LENGTH {
            assert!(token_parts(&valid[..TOKEN_LENGTH - removed]).is_none());
        }
        assert!(token_parts(&(valid + "A")).is_none());
        Ok(())
    }
}
