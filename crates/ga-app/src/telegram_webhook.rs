//! Bound Telegram webhook ingress for the configured private bot.

use std::sync::Arc;

use axum::{
    Json, Router,
    body::to_bytes,
    extract::{Request, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
};
use ga_db::{
    Database,
    channel_ingress::{IngressError, telegram_poll::TelegramPollError},
    telegram_cursor::TelegramCursorError,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use time::OffsetDateTime;
use uuid::Uuid;

use crate::telegram_input::TelegramBinding;

const SECRET_HEADER: &str = "x-telegram-bot-api-secret-token";
const BODY_LIMIT: usize = 1024 * 1024;

#[derive(Clone)]
pub struct TelegramWebhook {
    database: Database,
    secret: [u8; 32],
    owner_id: Uuid,
    external_owner_id: i64,
    instance: Arc<str>,
}

impl TelegramWebhook {
    /// Construct an enabled receiver from a trusted channel configuration.
    pub fn new(database: Database, binding: TelegramBinding<'_>, secret: &str) -> Option<Self> {
        if !(16..=256).contains(&secret.len())
            || !secret
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
            || !(1..(1_i64 << 52)).contains(&binding.external_owner_id)
            || !(1..=200).contains(&binding.instance_id.chars().count())
            || binding.instance_id.contains('\0')
        {
            return None;
        }
        Some(Self {
            database,
            secret: Sha256::digest(secret).into(),
            owner_id: binding.owner_id,
            external_owner_id: binding.external_owner_id,
            instance: binding.instance_id.into(),
        })
    }

    pub fn router(self) -> Router {
        Router::new()
            .route("/telegram/webhook", post(receive))
            .with_state(self)
    }

    fn verifies(&self, headers: &HeaderMap) -> bool {
        let mut values = headers.get_all(SECRET_HEADER).iter();
        let Some(provided) = values.next() else {
            return false;
        };
        let digest: [u8; 32] = Sha256::digest(provided.as_bytes()).into();
        values.next().is_none() && bool::from(self.secret.ct_eq(&digest))
    }
}

async fn receive(State(receiver): State<TelegramWebhook>, request: Request) -> Response {
    if !receiver.verifies(request.headers()) {
        return failure(StatusCode::FORBIDDEN, "Invalid webhook secret");
    }
    let body = match to_bytes(request.into_body(), BODY_LIMIT).await {
        Ok(body) => body,
        Err(_) => return failure(StatusCode::PAYLOAD_TOO_LARGE, "Update too large"),
    };
    let update: Value = match serde_json::from_slice(&body) {
        Ok(update) => update,
        Err(_) => return failure(StatusCode::BAD_REQUEST, "Invalid Telegram update"),
    };
    let received = OffsetDateTime::now_utc()
        .unix_timestamp_nanos()
        .div_euclid(1_000);
    let Some(received) = i64::try_from(received)
        .ok()
        .and_then(|received| StoredInstant::from_unix_microseconds(received).ok())
    else {
        return failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "Database unavailable or identity is not ready",
        );
    };
    let binding = TelegramBinding {
        owner_id: receiver.owner_id,
        external_owner_id: receiver.external_owner_id,
        instance_id: &receiver.instance,
    };
    match binding
        .accept_webhook_update(&receiver.database, &update, received)
        .await
    {
        Ok(accepted) => Json(json!({"ok": true, "accepted": accepted})).into_response(),
        Err(TelegramPollError::InvalidBinding) => {
            failure(StatusCode::BAD_REQUEST, "Invalid Telegram update")
        }
        Err(TelegramPollError::ChannelDisabled) => failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "Telegram channel is disabled",
        ),
        Err(
            TelegramPollError::Busy
            | TelegramPollError::Cursor(TelegramCursorError::Busy)
            | TelegramPollError::Ingress(IngressError::Busy),
        ) => failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "Telegram ingestion busy; retry delivery",
        ),
        Err(_) => failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "Database unavailable or identity is not ready",
        ),
    }
}

fn failure(status: StatusCode, detail: &'static str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}
