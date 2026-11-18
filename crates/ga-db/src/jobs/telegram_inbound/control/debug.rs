use ga_domain::{
    channel::InboundEnvelope, telegram_control::COMMAND_WHITESPACE, timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};

use super::TelegramControlError;
use crate::{outbox_claims::control_reply_fence, replay::REPLAY_MUTATION_LOCK};

const KEY: &str = "telegram:debug";
const ENABLED: &str = "Диагностика включена. Буду сообщать об ошибках, объединяя повторяющиеся уведомления. Тексты сообщений, показатели и секреты в уведомления не попадают.";
const DISABLED: &str = "Диагностика выключена. Включить уведомления об ошибках: /debug on";
const USAGE: &str = "Используйте /debug, /debug on или /debug off.";

pub(super) async fn apply(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
    ordering_epoch: i64,
) -> Result<(String, String), TelegramControlError> {
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(TelegramControlError::Busy);
    }
    let mut arguments = envelope
        .text()
        .ok_or(TelegramControlError::Malformed)?
        .split(|character| COMMAND_WHITESPACE.contains(character))
        .filter(|part| !part.is_empty())
        .skip(1);
    let first = arguments.next();
    let second = arguments.next();
    let mut value = read(txn).await?;
    let text = match (first, second) {
        (None | Some("on" | "off"), None) => {
            if let Some(setting) = first {
                let received = StoredInstant::try_from(envelope.received_at())
                    .map_err(|_| TelegramControlError::Malformed)?;
                let claimed = envelope
                    .occurred_at()
                    .map(StoredInstant::try_from)
                    .transpose()
                    .map_err(|_| TelegramControlError::Malformed)?
                    .unwrap_or(received);
                let message_at = claimed.unix_microseconds().div_euclid(1_000_000);
                let received_us = received.unix_microseconds();
                let update_id: i64 = envelope
                    .external_event_id()
                    .parse()
                    .map_err(|_| TelegramControlError::Malformed)?;
                let channel_id = format!("telegram:{}", envelope.channel().instance_id());
                let previous_at = number(&value, "message_at", -1);
                let previous_channel = value["channel_instance_id"]
                    .as_str()
                    .unwrap_or("telegram:primary");
                let wins_tie = if channel_id == previous_channel {
                    (ordering_epoch, update_id)
                        > (
                            number(&value, "ordering_epoch", 0),
                            number(&value, "update_id", -1),
                        )
                } else {
                    (received_us, channel_id.as_str())
                        > (number(&value, "received_us", -1), previous_channel)
                };
                if message_at > previous_at || (message_at == previous_at && wins_tie) {
                    value = json!({
                        "enabled": setting == "on", "update_id": update_id, "ordering_epoch": ordering_epoch,
                        "message_at": message_at, "channel_instance_id": channel_id, "received_us": received_us,
                    });
                    sqlx::query(
                        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
                                 ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    )
                    .bind(KEY)
                    .bind(value.to_string())
                    .execute(&mut **txn)
                    .await?;
                }
            }
            if value["enabled"] == true {
                ENABLED
            } else {
                DISABLED
            }
        }
        _ => USAGE,
    };
    let fence = control_reply_fence(
        txn,
        envelope.conversation_id(),
        json!({
            "telegram_debug": [
                value["message_at"], value["update_id"], value["ordering_epoch"],
                value["received_us"], value["channel_instance_id"],
            ],
        }),
    )
    .await?;
    Ok((text.to_owned(), fence))
}

async fn read(txn: &mut Transaction<'_, Postgres>) -> Result<Value, sqlx::Error> {
    let value: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(KEY)
            .fetch_optional(&mut **txn)
            .await?;
    value
        .map(|value| {
            serde_json::from_str(&value).map_err(|error| sqlx::Error::Decode(Box::new(error)))
        })
        .transpose()
        .map(|value| value.unwrap_or_default())
}

fn number(state: &Value, field: &str, fallback: i64) -> i64 {
    state[field].as_i64().unwrap_or(fallback)
}
