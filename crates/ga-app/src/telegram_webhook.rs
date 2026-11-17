//! Bound Telegram webhook ingress for the configured private bot.

use std::{env, sync::Arc, time::Duration};

use crate::telegram_settings::{TelegramIngressSettings, TelegramSettingsError};
use axum::{
    Json, Router,
    body::to_bytes,
    extract::{Request, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::post,
};
use ga_db::{
    Database,
    channel_ingress::{IngressError, telegram_poll::TelegramPollError},
    telegram_binding::TelegramBindingError,
    telegram_cursor::TelegramCursorError,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use time::OffsetDateTime;

const SECRET_HEADER: &str = "x-telegram-bot-api-secret-token";
const BODY_LIMIT: usize = 1024 * 1024;

#[derive(Clone)]
pub struct TelegramWebhook {
    database: Option<Database>,
    secret: Option<[u8; 32]>,
    settings: Option<Arc<TelegramIngressSettings>>,
}

impl TelegramWebhook {
    pub fn configured(database: Option<Database>) -> Self {
        let [secret, chat, integrations] = webhook_settings();
        Self::from_settings(
            database,
            secret.as_deref(),
            chat.as_deref(),
            integrations.as_deref(),
        )
    }

    /// Build ingress from the service configuration. A webhook always requires
    /// an existing, confirmed database pairing before it can record a delivery.
    pub fn from_settings(
        database: Option<Database>,
        secret: Option<&str>,
        chat: Option<&str>,
        integrations: Option<&str>,
    ) -> Self {
        let secret = secret
            .filter(|secret| {
                (16..=256).contains(&secret.len())
                    && secret
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
            })
            .map(|secret| Sha256::digest(secret).into());
        let settings = TelegramIngressSettings::for_webhook(chat, integrations)
            .ok()
            .flatten()
            .map(Arc::new);
        Self {
            database,
            secret,
            settings,
        }
    }

    pub fn router(self) -> Router {
        Router::new()
            .route(
                "/telegram/webhook",
                post(receive).fallback(method_not_allowed),
            )
            .with_state(self)
    }

    fn verifies(&self, headers: &HeaderMap) -> bool {
        let Some(secret) = self.secret else {
            return false;
        };
        let mut values = headers.get_all(SECRET_HEADER).iter();
        let Some(provided) = values.next() else {
            return false;
        };
        let digest: [u8; 32] = Sha256::digest(provided.as_bytes()).into();
        values.next().is_none() && bool::from(secret.ct_eq(&digest))
    }
}

async fn receive(State(receiver): State<TelegramWebhook>, request: Request) -> Response {
    if !receiver.verifies(request.headers()) {
        return failure(StatusCode::FORBIDDEN, "Invalid webhook secret");
    }
    let Some(settings) = receiver.settings.as_deref() else {
        return failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "Telegram integration is disabled",
        );
    };
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
        return database_unavailable();
    };
    let Some(database) = receiver.database.as_ref() else {
        return database_unavailable();
    };
    let accepted = tokio::time::timeout(Duration::from_millis(1800), async {
        let binding = settings
            .confirmed_binding(database)
            .await
            .map_err(|error| match error {
                TelegramSettingsError::Pairing(error) => TelegramPollError::Pairing(error),
                _ => TelegramPollError::InvalidSelection,
            })?;
        binding
            .accept_webhook_update(database, &update, received)
            .await
    })
    .await;
    match accepted {
        Ok(Ok(accepted)) => Json(json!({"ok": true, "accepted": accepted})).into_response(),
        Ok(Err(TelegramPollError::InvalidBinding)) => {
            failure(StatusCode::BAD_REQUEST, "Invalid Telegram update")
        }
        Ok(Err(TelegramPollError::ChannelDisabled)) => failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "Telegram channel is disabled",
        ),
        Ok(Err(
            TelegramPollError::Busy
            | TelegramPollError::Pairing(TelegramBindingError::Busy)
            | TelegramPollError::Cursor(TelegramCursorError::Busy)
            | TelegramPollError::Ingress(IngressError::Busy),
        )) => failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "Telegram ingestion busy; retry delivery",
        ),
        _ => database_unavailable(),
    }
}

async fn method_not_allowed() -> Response {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        [(header::ALLOW, "POST")],
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response()
}

fn database_unavailable() -> Response {
    failure(
        StatusCode::SERVICE_UNAVAILABLE,
        "Database unavailable or identity is not ready",
    )
}

fn webhook_settings() -> [Option<String>; 3] {
    const NAMES: [&str; 3] = [
        "GA_TELEGRAM_WEBHOOK_SECRET",
        "GA_TELEGRAM_USER_ID",
        "GA_INTEGRATIONS",
    ];
    let index = |name: &str| {
        NAMES
            .iter()
            .position(|setting| name.eq_ignore_ascii_case(setting))
    };
    let mut settings = [None, None, None];
    if let Ok(entries) = dotenvy::from_path_iter(".env") {
        for (name, value) in entries.filter_map(Result::ok) {
            if let Some(index) = index(&name) {
                settings[index] = Some(value);
            }
        }
    }
    for (name, value) in env::vars_os() {
        if let Some(index) = name.to_str().and_then(index) {
            settings[index] = Some(value.into_string().unwrap_or_default());
        }
    }
    settings
}

fn failure(status: StatusCode, detail: &'static str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}
