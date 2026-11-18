use ga_domain::{channel::legacy_telegram_action_id, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{ALL_HISTORY, EXPIRED_HISTORY, OPEN_MIGRAINES};
use crate::channel_ingress::{ChannelCallback, IngressError};

/// Earlier Telegram messages carry a database-backed selector with an h: prefix.
pub(crate) async fn resolve_legacy_history(
    txn: &mut Transaction<'_, Postgres>,
    callback: &ChannelCallback<'_>,
) -> Result<Option<Value>, IngressError> {
    let Some(token) = callback
        .token
        .strip_prefix("h:")
        .filter(|_| callback.channel == "telegram")
    else {
        return Ok(None);
    };
    let update: i64 = callback
        .event_id
        .parse()
        .map_err(|_| IngressError::InvalidEnvelope)?;
    let current = if (1..=32).contains(&token.len())
        && token
            .bytes()
            .all(|part| part.is_ascii_alphanumeric() || matches!(part, b'-' | b'_'))
    {
        sqlx::query_scalar::<_, String>("SELECT value::text FROM app_state WHERE key=$1")
            .bind(format!("telegram:selection:{token}"))
            .fetch_optional(&mut **txn)
            .await?
            .and_then(|value| serde_json::from_str::<Value>(&value).ok())
    } else {
        None
    };
    let destination = format!("telegram:{}", callback.instance_id);
    let current = current.as_ref().filter(|value| {
        let channel = value
            .get("channel_instance_id")
            .map_or(Some("telegram:primary"), Value::as_str);
        channel == Some(destination.as_str())
            && StoredInstant::from_legacy_input(&value["expires_at"])
                .is_some_and(|expires_at| expires_at > callback.received_at)
    });
    let action = current
        .and_then(history_action)
        .unwrap_or_else(|| EXPIRED_HISTORY.into());
    Ok(Some(json!({
        "action_id":action,"label":"История дневника",
        "operation_id":legacy_telegram_action_id(update).to_string(),
        "token":null,"expires_at":null,
    })))
}

fn history_action(value: &Value) -> Option<String> {
    let action = value["action"].as_str()?;
    if action == "page" {
        let action = if value["open_only"] == true {
            OPEN_MIGRAINES
        } else {
            ALL_HISTORY
        };
        if value["cursor"].is_null() {
            return Some(action.into());
        }
        let [at, id] = value["cursor"].as_array()?.as_slice() else {
            return None;
        };
        let at = StoredInstant::from_legacy_input(at)?;
        let id: Uuid = id.as_str()?.parse().ok()?;
        return Some(format!("{action}/{}/{id}", at.unix_microseconds()));
    }
    if !matches!(action, "edit" | "close" | "delete") {
        return None;
    }
    let id: Uuid = value["event_id"].as_str()?.parse().ok()?;
    let revision = value["revision"]
        .as_i64()
        .and_then(|part| i32::try_from(part).ok())?;
    (revision > 0).then(|| format!("diary.history.{action}/{id}/{revision}"))
}
