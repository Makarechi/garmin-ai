use std::{
    error::Error,
    io,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

use axum::{
    Router,
    body::to_bytes,
    extract::{Request, State},
    response::{IntoResponse, Response},
};
use ga_archive::GarminTokenFile;
use ga_domain::identity::GarminProfileId;
use reqwest::{
    Method, StatusCode,
    header::{AUTHORIZATION, CONTENT_TYPE, COOKIE, HeaderValue, RETRY_AFTER, SET_COOKIE},
};
use serde_json::{Value, json};
use time::{Date, Month};

use super::{
    DI_PATH, DailyEndpoint, Hosts, LegacyLogin, LegacyRead, LegacyReader, LoginProgress,
    PROFILE_PATH, RestorePolicy, session::Native,
};
use crate::{
    GarminError, Url,
    tests::{Server, test_config},
};

const USER: &str = "synthetic-garmin-account@example.invalid";
const PASSWORD: &str = "synthetic-garmin-password";
const CODE: &str = "123456";
const INITIAL_ACCESS: &str = "synthetic-legacy-access";
const INITIAL_REFRESH: &str = "synthetic-legacy-refresh";
const ROTATED_ACCESS: &str = "synthetic-rotated-access";
const ROTATED_REFRESH: &str = "synthetic-rotated-refresh";
const CLIENT_ID: &str = "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2";

#[derive(Default)]
struct Target {
    start: AtomicUsize,
    mfa: AtomicUsize,
    grant: AtomicUsize,
    profile: AtomicUsize,
    mode: AtomicUsize,
    reads: AtomicUsize,
}

impl Target {
    fn calls(&self) -> [usize; 4] {
        [&self.start, &self.mfa, &self.grant, &self.profile]
            .map(|counter| counter.load(Ordering::Relaxed))
    }
}

async fn target() -> Result<(Arc<Target>, Server, Hosts), Box<dyn Error>> {
    let state = Arc::new(Target::default());
    let server = Server::start(Router::new().fallback(handle).with_state(state.clone())).await?;
    let hosts = Hosts {
        sso: server.base.join("sso/")?,
        di: server.base.join("di/")?,
        api: server.base.join("api/")?,
    };
    Ok((state, server, hosts))
}

fn ok_json(value: Value) -> Response {
    ([(CONTENT_TYPE, "application/json")], value.to_string()).into_response()
}

async fn handle(State(state): State<Arc<Target>>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let path = parts.uri.path();
    let mode = state.mode.load(Ordering::Relaxed);
    let auth = parts
        .headers
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    let cookie = parts
        .headers
        .get(COOKIE)
        .and_then(|value| value.to_str().ok());
    let Ok(body) = to_bytes(body, 2048).await else {
        return StatusCode::PAYLOAD_TOO_LARGE.into_response();
    };
    let read_json = || serde_json::from_slice::<Value>(&body).unwrap_or_default();
    let mut parsed = Url::parse("http://127.0.0.1/").ok();
    if path == "/sso/mobile/api/login" && parts.method == Method::POST {
        state.start.fetch_add(1, Ordering::Relaxed);
        let json = read_json();
        if auth.is_some() || json["username"] != USER || json["password"] != PASSWORD {
            return ok_json(json!({"responseStatus": {"type": "INVALID_USERNAME_PASSWORD"}}));
        }
        if mode == 2 {
            return ok_json(json!({"error":{"status-code":"429"}}));
        }
        if mode == 3 {
            return ok_json(
                json!({"responseStatus":{"type":"SUCCESSFUL"},"serviceTicketId":"synthetic-ticket"}),
            );
        }
        let mut response = ok_json(json!({
            "responseStatus": {"type": "MFA_REQUIRED"},
            "customerMfaInfo": {"mfaLastMethodUsed": "email"},
        }));
        response.headers_mut().insert(
            SET_COOKIE,
            HeaderValue::from_static("mfa=synthetic; Path=/sso/; HttpOnly"),
        );
        return response;
    }
    if path == "/sso/mobile/api/mfa/verifyCode" && parts.method == Method::POST {
        state.mfa.fetch_add(1, Ordering::Relaxed);
        let json = read_json();
        if auth.is_some() || cookie != Some("mfa=synthetic") || json["mfaMethod"] != "email" {
            return StatusCode::FORBIDDEN.into_response();
        }
        if json["mfaVerificationCode"] != CODE {
            return ok_json(json!({"responseStatus":{"type":"INVALID_MFA_CODE"}}));
        }
        return ok_json(
            json!({"responseStatus":{"type":"SUCCESSFUL"},"serviceTicketId":"synthetic-ticket"}),
        );
    }
    if path.strip_prefix("/di/") == Some(DI_PATH) && parts.method == Method::POST {
        state.grant.fetch_add(1, Ordering::Relaxed);
        if cookie.is_some() || !auth.is_some_and(|value| value.starts_with("Basic ")) {
            return StatusCode::FORBIDDEN.into_response();
        }
        let Some(url) = parsed.as_mut() else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        let Ok(query) = std::str::from_utf8(&body) else {
            return StatusCode::BAD_REQUEST.into_response();
        };
        url.set_query(Some(query));
        let pairs: std::collections::BTreeMap<_, _> = url.query_pairs().into_owned().collect();
        let field = |name| pairs.get(name).map(String::as_str);
        if field("client_id") != Some(CLIENT_ID) {
            return StatusCode::BAD_REQUEST.into_response();
        }
        return match field("grant_type") {
            Some("refresh_token") if field("refresh_token") == Some(INITIAL_REFRESH) => {
                ok_json(json!({"access_token":ROTATED_ACCESS,"refresh_token":ROTATED_REFRESH}))
            }
            Some("https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket")
                if field("service_ticket") == Some("synthetic-ticket")
                    && field("service_url")
                        == Some("https://mobile.integration.garmin.com/gcm/ios") =>
            {
                ok_json(json!({"access_token":INITIAL_ACCESS,"refresh_token":INITIAL_REFRESH}))
            }
            _ => StatusCode::BAD_REQUEST.into_response(),
        };
    }
    if path.strip_prefix("/api/") == Some(PROFILE_PATH) && parts.method == Method::GET {
        state.profile.fetch_add(1, Ordering::Relaxed);
        if cookie.is_some()
            || ![INITIAL_ACCESS, ROTATED_ACCESS]
                .iter()
                .any(|value| auth == Some(&format!("Bearer {value}")))
        {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        if mode == 4 && auth == Some("Bearer synthetic-legacy-access") {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        return ok_json(
            json!({"profileId": if mode == 1 { 99999 } else { 12345 }, "displayName": "Synthetic Profile"}),
        );
    }
    if path.starts_with("/api/") && parts.method == Method::GET {
        state.reads.fetch_add(1, Ordering::Relaxed);
        if cookie.is_some()
            || ![INITIAL_ACCESS, ROTATED_ACCESS]
                .iter()
                .any(|value| auth == Some(&format!("Bearer {value}")))
        {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        if mode == 5 && auth == Some("Bearer synthetic-legacy-access") {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        if mode == 6 {
            return (StatusCode::TOO_MANY_REQUESTS, [(RETRY_AFTER, "60")]).into_response();
        }
        if path == "/api/usersummary-service/usersummary/daily/Synthetic%20Profile"
            && parts.uri.query() == Some("calendarDate=2026-11-01")
        {
            return br#"{ "unknown" : 123 }"#.as_slice().into_response();
        }
        if path == "/api/download-service/files/activity/42" {
            return b"PK\x03\x04synthetic".as_slice().into_response();
        }
    }
    StatusCode::NOT_FOUND.into_response()
}

fn expected_owner() -> Result<String, Box<dyn Error>> {
    let owner: GarminProfileId = "12345".parse()?;
    Ok(owner.fingerprint())
}

#[tokio::test]
async fn login_mfa_save_restart_and_refresh_keep_verified_owner() -> Result<(), Box<dyn Error>> {
    let (target, _server, hosts) = target().await?;
    let login = LegacyLogin::with_hosts(hosts.clone(), test_config())?;
    let LoginProgress::Mfa(mut mfa) = login.start(USER, PASSWORD).await? else {
        return Err(io::Error::other("Expected MFA in synthetic fixture").into());
    };
    assert!(matches!(
        mfa.verify("000000").await,
        Err(GarminError::InvalidMfaCode)
    ));
    let authenticated = mfa.verify(CODE).await?;
    assert!(matches!(
        mfa.verify(CODE).await,
        Err(GarminError::MfaExpired)
    ));
    let expected = expected_owner()?;
    assert_eq!(authenticated.fingerprint(), expected);
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?.join("tokens");
    let store = GarminTokenFile::open(&root)?;
    assert_eq!(
        authenticated.save_for_owner(&store, "other-owner"),
        Err(GarminError::OwnerMismatch)
    );
    assert!(store.try_lock()?.read()?.is_none());
    authenticated.save_for_owner(&store, &expected)?;
    drop(authenticated);
    drop(store);
    let reopened = GarminTokenFile::open(&root)?;
    let native = Native::new(hosts, test_config())?;
    let old = reopened
        .try_lock()?
        .read()?
        .ok_or_else(|| io::Error::other("Expected synthetic session"))?;
    let old_sdk_shape: Value = serde_json::from_slice(&old)?;
    assert_eq!(
        old_sdk_shape,
        json!({"di_token":INITIAL_ACCESS,"di_refresh_token":INITIAL_REFRESH,"di_client_id":CLIENT_ID})
    );
    assert_eq!(
        native
            .restore(&reopened, &expected, RestorePolicy::IfNeeded)
            .await?
            .fingerprint(),
        expected
    );
    target.mode.store(4, Ordering::Relaxed);
    let rotated = native
        .restore(&reopened, &expected, RestorePolicy::IfNeeded)
        .await?;
    assert_eq!(rotated.fingerprint(), expected);
    let newer = reopened
        .try_lock()?
        .read()?
        .ok_or_else(|| io::Error::other("Expected rotated session"))?;
    assert_eq!(
        serde_json::from_slice::<Value>(&newer)?,
        json!({"di_token":ROTATED_ACCESS,"di_refresh_token":ROTATED_REFRESH,"di_client_id":CLIENT_ID})
    );
    assert_eq!(target.calls(), [1, 2, 2, 4]);
    Ok(())
}

#[tokio::test]
async fn account_mismatch_does_not_replace_the_previous_token_file() -> Result<(), Box<dyn Error>> {
    let (target, _server, hosts) = target().await?;
    target.mode.store(3, Ordering::Relaxed);
    let LoginProgress::Complete(authenticated) =
        LegacyLogin::with_hosts(hosts.clone(), test_config())?
            .start(USER, PASSWORD)
            .await?
    else {
        return Err(io::Error::other("Expected direct login").into());
    };
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?.join("tokens");
    let store = GarminTokenFile::open(&root)?;
    let expected = expected_owner()?;
    authenticated.save_for_owner(&store, &expected)?;
    let original = store.try_lock()?.read()?;
    target.mode.store(1, Ordering::Relaxed);
    let native = Native::new(hosts, test_config())?;
    assert!(matches!(
        native
            .restore(&store, &expected, RestorePolicy::Refresh)
            .await,
        Err(GarminError::OwnerMismatch)
    ));
    assert_eq!(store.try_lock()?.read()?, original);
    Ok(())
}

#[tokio::test]
async fn rejects_bad_password_and_bounds_attempts_and_rate_limit() -> Result<(), Box<dyn Error>> {
    let (target, _server, hosts) = target().await?;
    let login = LegacyLogin::with_hosts(hosts.clone(), test_config())?;
    assert!(matches!(
        login.start(USER, "synthetic wrong password").await,
        Err(GarminError::AuthenticationRequired)
    ));
    target.mode.store(2, Ordering::Relaxed);
    let login = LegacyLogin::with_hosts(hosts.clone(), test_config())?;
    assert!(matches!(
        login.start(USER, PASSWORD).await,
        Err(GarminError::RateLimited { .. })
    ));
    target.mode.store(0, Ordering::Relaxed);
    let LoginProgress::Mfa(mut mfa) = LegacyLogin::with_hosts(hosts, test_config())?
        .start(USER, PASSWORD)
        .await?
    else {
        return Err(io::Error::other("Expected MFA in synthetic fixture").into());
    };
    assert!(matches!(
        mfa.verify("<>").await,
        Err(GarminError::InvalidMfaCode)
    ));
    for _ in 0..3 {
        assert!(matches!(
            mfa.verify("000000").await,
            Err(GarminError::InvalidMfaCode)
        ));
    }
    assert!(matches!(
        mfa.verify(CODE).await,
        Err(GarminError::MfaExpired)
    ));
    assert_eq!(target.calls(), [3, 3, 0, 0]);
    Ok(())
}

#[tokio::test]
async fn reads_original_data_and_rotates_once_after_a_legacy_resource_rejects_the_token()
-> Result<(), Box<dyn Error>> {
    use std::{num::NonZeroU64, time::Duration};
    let (target, _server, hosts) = target().await?;
    target.mode.store(3, Ordering::Relaxed);
    let LoginProgress::Complete(authenticated) =
        LegacyLogin::with_hosts(hosts.clone(), test_config())?
            .start(USER, PASSWORD)
            .await?
    else {
        return Err(io::Error::other("Expected direct fixture login").into());
    };
    let temporary = tempfile::tempdir()?;
    let store = GarminTokenFile::open(&temporary.path().canonicalize()?.join("tokens"))?;
    let owner = expected_owner()?;
    authenticated.save_for_owner(&store, &owner)?;
    let reader = LegacyReader::with_hosts(store, &owner, test_config(), hosts).await?;
    let day = Date::from_calendar_date(2026, Month::November, 1)?;
    let request = LegacyRead::Daily(DailyEndpoint::Summary, day);
    target.mode.store(5, Ordering::Relaxed);
    assert_eq!(
        reader.get_json(request, 1024).await?.as_deref(),
        Some(br#"{ "unknown" : 123 }"#.as_slice())
    );
    let mut destination = Vec::new();
    reader
        .download_activity(NonZeroU64::try_from(42)?, 1024, &mut destination)
        .await?;
    assert_eq!(destination, b"PK\x03\x04synthetic");
    assert_eq!(target.calls(), [1, 0, 2, 3]);
    target.mode.store(6, Ordering::Relaxed);
    assert_eq!(
        reader.get_json(request, 1024).await,
        Err(GarminError::RateLimited {
            retry_after: Duration::from_secs(60)
        })
    );
    target.mode.store(0, Ordering::Relaxed);
    assert!(matches!(
        reader.clone().get_json(request, 1024).await,
        Err(GarminError::RateLimited { .. })
    ));
    assert_eq!(target.reads.load(Ordering::Relaxed), 4);
    Ok(())
}
