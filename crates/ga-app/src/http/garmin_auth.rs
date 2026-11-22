use std::{path::PathBuf, sync::Arc};

use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::any,
};
use ga_app::{
    garmin_mini_app::{GarminMiniApp, GarminMiniAppError, GarminMiniAppSettings},
    garmin_worker::{garmin_auth_source, garmin_auth_url},
    telegram_settings::TelegramIngressSettings,
};
use ga_db::{
    Database,
    garmin_integration::{GarminIntegrationError, GarminReauthError},
    garmin_owner::OwnerVerificationError,
    telegram_binding::TelegramBindingError,
};
use ga_garmin::{GarminError, managed_password::GarminPasswordSecret};
use serde::Deserialize;
use serde_json::json;

use super::{
    method_not_allowed,
    request_json::decode_json,
    tools::{detail, invalid_arguments},
};
use crate::configuration;

const PAGE: &str = include_str!("../../../../src/garmin_ai/static/garmin_auth/index.html");
const CONTENT_POLICY: &str = concat!(
    "default-src 'none'; script-src 'unsafe-inline' https://telegram.org; ",
    "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'",
);
const REQUEST_LIMIT: usize = 64 * 1024;

type MiniApp = Option<Arc<GarminMiniApp>>;

pub(super) fn router(database: Option<Database>) -> Router {
    Router::new()
        .route("/garmin-auth", any(form))
        .route("/garmin-auth/start", any(start))
        .route("/garmin-auth/complete", any(complete))
        .with_state(database.and_then(configured).map(Arc::new))
}

fn configured(database: Database) -> Option<GarminMiniApp> {
    let [
        url,
        token,
        owner,
        integrations,
        email,
        secret,
        data,
        tokens,
        locks,
    ] = configuration::read([
        "GA_GARMIN_AUTH_URL",
        "GA_TELEGRAM_BOT_TOKEN",
        "GA_TELEGRAM_USER_ID",
        "GA_INTEGRATIONS",
        "GA_GARMIN_EMAIL",
        "GA_GARMIN_PASSWORD_SECRET_VERSION",
        "GA_DATA_DIR",
        "GA_TOKEN_DIR",
        "GA_LOCK_DIR",
    ])
    .ok()?;
    garmin_auth_url(url.as_deref()?)?;
    let telegram = TelegramIngressSettings::selected(owner.as_deref(), integrations.as_deref())
        .ok()
        .flatten()?;
    if telegram.external_owner_id() == 0 {
        return None;
    }
    let bot_token = token.filter(|token| !token.is_empty())?;
    let data_directory = PathBuf::from(data.unwrap_or_else(|| "data".into()));
    let token_directory = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
    let lock_directory =
        configuration::lock_directory(&data_directory, &token_directory, locks.map(PathBuf::from))
            .ok()?;
    let garmin_instance = garmin_auth_source(integrations.as_deref(), &token_directory)
        .ok()
        .flatten()?;
    let settings = GarminMiniAppSettings {
        telegram,
        bot_token,
        garmin_instance,
        email: email.filter(|email| !email.is_empty()),
        password: secret.and_then(|value| GarminPasswordSecret::pinned(value).ok()),
        token_directory,
        lock_directory,
    };
    Some(GarminMiniApp::new(database, settings))
}

async fn form(State(app): State<MiniApp>, method: Method) -> Response {
    let result = if method != Method::GET {
        method_not_allowed("GET")
    } else if app.is_none() {
        detail(StatusCode::NOT_FOUND, "Unavailable")
    } else {
        (
            [
                (header::CONTENT_TYPE, "text/html; charset=utf-8"),
                (header::REFERRER_POLICY, "no-referrer"),
                (header::CONTENT_SECURITY_POLICY, CONTENT_POLICY),
            ],
            PAGE,
        )
            .into_response()
    };
    no_cache(result)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthRequest {
    init_data: String,
    #[serde(default)]
    code: String,
}

async fn start(State(app): State<MiniApp>, request: Request) -> Response {
    no_cache(change(app, request, false).await)
}

async fn complete(State(app): State<MiniApp>, request: Request) -> Response {
    no_cache(change(app, request, true).await)
}

async fn change(app: MiniApp, request: Request, completing: bool) -> Response {
    if request.method() != Method::POST {
        return method_not_allowed("POST");
    }
    let Some(body) = parse_request(request.into_body()).await else {
        return invalid_arguments();
    };
    let Some(app) = app else {
        return detail(StatusCode::NOT_FOUND, "Unavailable");
    };
    let result = if completing {
        app.complete(&body.init_data, &body.code).await
    } else {
        app.start(&body.init_data).await
    };
    match result {
        Ok(result) => Json(json!({"status":result.label()})).into_response(),
        Err(error) => failure(error, completing),
    }
}

async fn parse_request(body: Body) -> Option<AuthRequest> {
    let bytes = to_bytes(body, REQUEST_LIMIT).await.ok()?;
    let document = decode_json(&bytes)?;
    let request: AuthRequest = serde_json::from_str(&document).ok()?;
    (request.init_data.chars().count() <= 8192 && request.code.chars().count() <= 20)
        .then_some(request)
}

fn failure(error: GarminMiniAppError, completing: bool) -> Response {
    use StatusCode as S;
    let unavailable = (S::CONFLICT, "Этот способ входа сейчас недоступен.");
    let (status, message) = match error {
        GarminMiniAppError::Session => (
            S::FORBIDDEN,
            "Сессия Telegram истекла. Откройте форму заново.",
        ),
        GarminMiniAppError::Code => (S::BAD_REQUEST, "Код неверен или истёк. Попробуйте снова."),
        GarminMiniAppError::TooSoon => (
            S::TOO_MANY_REQUESTS,
            "Подождите минуту перед запросом нового кода.",
        ),
        GarminMiniAppError::Unavailable
        | GarminMiniAppError::Database(GarminReauthError::Unavailable) => unavailable,
        GarminMiniAppError::Database(GarminReauthError::Pairing(
            TelegramBindingError::Missing | TelegramBindingError::Mismatch,
        )) => (
            S::CONFLICT,
            "Владелец Telegram не совпадает с настройкой сервера.",
        ),
        GarminMiniAppError::Database(GarminReauthError::Integration(
            GarminIntegrationError::Owner(
                OwnerVerificationError::EnrollmentRequired
                | OwnerVerificationError::Mismatch
                | OwnerVerificationError::InvalidStoredBinding,
            ),
        )) => (
            S::CONFLICT,
            "Владельца Garmin нужно подтвердить на сервере.",
        ),
        GarminMiniAppError::Password => (
            S::SERVICE_UNAVAILABLE,
            "Сервер не смог получить сохранённый пароль Garmin.",
        ),
        GarminMiniAppError::Provider(GarminError::RateLimited { .. }) => (
            S::TOO_MANY_REQUESTS,
            "Garmin временно ограничил вход. Подождите и попробуйте позже.",
        ),
        GarminMiniAppError::Provider(
            GarminError::Timeout
            | GarminError::Network
            | GarminError::Upstream(_)
            | GarminError::InterruptedTransfer,
        ) => (
            S::SERVICE_UNAVAILABLE,
            "Не удалось связаться с Garmin. Попробуйте позже.",
        ),
        GarminMiniAppError::Provider(
            GarminError::AuthenticationRequired | GarminError::Unauthorized,
        ) => (
            S::UNAUTHORIZED,
            "Garmin не подтвердил вход приложения. Проверьте вход на официальном сайте Garmin Connect.",
        ),
        GarminMiniAppError::Database(GarminReauthError::Publication) if completing => (
            S::SERVICE_UNAVAILABLE,
            "Не удалось завершить вход в Garmin. Попробуйте ещё раз.",
        ),
        GarminMiniAppError::Database(
            GarminReauthError::Channel(_)
            | GarminReauthError::Integration(_)
            | GarminReauthError::Pairing(_),
        ) => (S::SERVICE_UNAVAILABLE, "База данных временно недоступна."),
        _ if completing => (
            S::SERVICE_UNAVAILABLE,
            "Не удалось завершить вход в Garmin. Попробуйте ещё раз.",
        ),
        _ => (
            S::SERVICE_UNAVAILABLE,
            "Не удалось запросить код Garmin. Попробуйте позже.",
        ),
    };
    detail(status, message)
}

fn no_cache(mut response: Response) -> Response {
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("no-store"),
    );
    response
}
