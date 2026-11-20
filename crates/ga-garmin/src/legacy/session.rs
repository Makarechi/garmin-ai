use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::{
    Engine,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use ga_archive::{GarminTokenFile, GarminTokenGuard};
use ga_domain::identity::GarminProfileId;
use reqwest::{
    Client, RequestBuilder, Response, Url,
    header::{ACCEPT, AUTHORIZATION, CACHE_CONTROL},
};
use serde::{Deserialize, Serialize};

use super::{
    DI_PATH, Hosts, MAX_AUTH_BYTES, MAX_PROFILE_BYTES, PROFILE_PATH, endpoint, native_headers,
};
use crate::{
    GarminError, Schedule, TransportConfig, build_http, read_bounded, send, sensitive_header,
    successful,
};

const DI_CLIENT_IDS: [&str; 4] = [
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
    "GARMIN_CONNECT_MOBILE_IOS_DI",
];
const DI_GRANT_TYPE: &str =
    "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket";

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum RestorePolicy {
    IfNeeded,
    Refresh,
}

struct Profile {
    id: GarminProfileId,
    display_name: Option<String>,
}

pub struct LegacySession {
    profile: Profile,
    tokens: Tokens,
}

#[derive(Deserialize, Serialize)]
struct Tokens {
    di_token: String,
    di_refresh_token: Option<String>,
    di_client_id: String,
}

#[derive(Deserialize)]
struct TokenGrant {
    access_token: String,
    refresh_token: Option<String>,
}

#[derive(Deserialize)]
struct TokenClaims {
    exp: Option<u64>,
    client_id: Option<String>,
}

pub(super) struct Native {
    pub(super) http: Client,
    pub(super) hosts: Hosts,
    pub(super) pacing: Duration,
}

impl LegacySession {
    pub fn fingerprint(&self) -> String {
        self.profile.id.fingerprint()
    }

    pub(super) fn display_name(&self) -> Option<&str> {
        self.profile.display_name.as_deref()
    }

    pub(super) fn expires_soon(&self) -> bool {
        self.tokens.expires_soon()
    }

    pub(super) fn authorized_read(
        &self,
        client: &Client,
        url: Url,
        accept: &str,
    ) -> Result<RequestBuilder, GarminError> {
        let bearer = sensitive_header("Bearer", &self.tokens.di_token)?;
        Ok(native_headers(client.get(url))
            .header(AUTHORIZATION, bearer)
            .header(ACCEPT, accept))
    }

    /// The application holds its owner and backup fences while publishing a new login.
    pub fn save_for_owner(
        &self,
        store: &GarminTokenFile,
        expected_fingerprint: &str,
    ) -> Result<(), GarminError> {
        if self.fingerprint() != expected_fingerprint {
            return Err(GarminError::OwnerMismatch);
        }
        let guard = store.try_lock().map_err(|_| GarminError::TokenStorage)?;
        guard
            .replace(&self.tokens.encode()?)
            .map_err(|_| GarminError::TokenStorage)
    }

    pub async fn restore(
        store: &GarminTokenFile,
        expected_fingerprint: &str,
        config: TransportConfig,
        policy: RestorePolicy,
    ) -> Result<Self, GarminError> {
        Native::new(Hosts::production()?, config)?
            .restore(store, expected_fingerprint, policy)
            .await
    }

    /// Read and authenticate saved credentials for explicit offline enrollment.
    /// A successful profile lookup alone never replaces the saved token file.
    pub async fn restore_for_local_enrollment(
        store: &GarminTokenFile,
        config: TransportConfig,
    ) -> Result<Self, GarminError> {
        let (session, _) = Self::restore_for_local_verification(store, config).await?;
        Ok(session)
    }

    /// Verify the profile without publishing any credentials until the caller
    /// has checked the local owner. The flag indicates a rotated session.
    pub async fn restore_for_local_verification(
        store: &GarminTokenFile,
        config: TransportConfig,
    ) -> Result<(Self, bool), GarminError> {
        let native = Native::new(Hosts::production()?, config)?;
        let guard = store.try_lock().map_err(|_| GarminError::TokenStorage)?;
        native
            .restore_candidate(&guard, RestorePolicy::IfNeeded, &mut Schedule::new())
            .await
    }
}

impl Tokens {
    fn encode(&self) -> Result<Vec<u8>, GarminError> {
        serde_json::to_vec(self).map_err(|_| GarminError::InvalidTokenResponse)
    }

    fn from_grant(
        grant: TokenGrant,
        previous_refresh: Option<String>,
        client: &str,
    ) -> Result<Self, GarminError> {
        if grant.access_token.is_empty() {
            return Err(GarminError::InvalidTokenResponse);
        }
        sensitive_header("Bearer", &grant.access_token)?;
        Ok(Self {
            di_client_id: claims(&grant.access_token)
                .and_then(|claims| claims.client_id)
                .filter(|id| !id.is_empty())
                .unwrap_or_else(|| client.to_owned()),
            di_token: grant.access_token,
            di_refresh_token: grant.refresh_token.or(previous_refresh),
        })
    }

    fn expires_soon(&self) -> bool {
        let exp = claims(&self.di_token).and_then(|claims| claims.exp);
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        exp.is_some_and(|exp| exp <= now.as_secs().saturating_add(900))
    }
}

fn claims(token: &str) -> Option<TokenClaims> {
    let mut sections = token.split('.');
    let header = URL_SAFE_NO_PAD.decode(sections.next()?).ok()?;
    let body = URL_SAFE_NO_PAD.decode(sections.next()?).ok()?;
    if sections.next().is_none_or(str::is_empty) || sections.next().is_some() {
        return None;
    }
    let header = serde_json::from_slice::<serde_json::Value>(&header).ok()?;
    if header.get("alg")?.as_str()?.eq_ignore_ascii_case("none") {
        return None;
    }
    serde_json::from_slice(&body).ok()
}

impl Native {
    pub(super) fn new(hosts: Hosts, config: TransportConfig) -> Result<Self, GarminError> {
        let https = hosts.di.scheme() == "https" && hosts.api.scheme() == "https";
        let http = build_http(config, https)?
            .build()
            .map_err(|_| GarminError::InvalidConfiguration)?;
        Ok(Self {
            http,
            hosts,
            pacing: config.pacing,
        })
    }

    pub(super) async fn login(
        &self,
        ticket: &str,
        service: &str,
    ) -> Result<LegacySession, GarminError> {
        let mut schedule = Schedule::new();
        for client in DI_CLIENT_IDS {
            let result = self
                .grant(
                    &mut schedule,
                    client,
                    &[
                        ("client_id", client),
                        ("service_ticket", ticket),
                        ("grant_type", DI_GRANT_TYPE),
                        ("service_url", service),
                    ],
                )
                .await;
            match result {
                Ok(grant) => {
                    let tokens = Tokens::from_grant(grant, None, client)?;
                    let profile = self.profile(&mut schedule, &tokens).await?;
                    return Ok(LegacySession { profile, tokens });
                }
                Err(
                    GarminError::Unauthorized
                    | GarminError::Forbidden
                    | GarminError::UnexpectedStatus(400),
                ) => continue,
                Err(error) => return Err(error),
            }
        }
        Err(GarminError::AuthenticationRequired)
    }

    pub(super) async fn restore(
        &self,
        store: &GarminTokenFile,
        expected_fingerprint: &str,
        policy: RestorePolicy,
    ) -> Result<LegacySession, GarminError> {
        self.restore_scheduled(store, expected_fingerprint, policy, &mut Schedule::new())
            .await
    }

    pub(super) async fn restore_scheduled(
        &self,
        store: &GarminTokenFile,
        expected_fingerprint: &str,
        policy: RestorePolicy,
        schedule: &mut Schedule,
    ) -> Result<LegacySession, GarminError> {
        let guard = store.try_lock().map_err(|_| GarminError::TokenStorage)?;
        let (session, rotated) = self.restore_candidate(&guard, policy, schedule).await?;
        if session.fingerprint() != expected_fingerprint {
            return Err(GarminError::OwnerMismatch);
        }
        if rotated {
            guard
                .replace(&session.tokens.encode()?)
                .map_err(|_| GarminError::TokenStorage)?;
        }
        Ok(session)
    }

    async fn restore_candidate(
        &self,
        guard: &GarminTokenGuard,
        policy: RestorePolicy,
        schedule: &mut Schedule,
    ) -> Result<(LegacySession, bool), GarminError> {
        let bytes = guard
            .read()
            .map_err(|_| GarminError::TokenStorage)?
            .ok_or(GarminError::AuthenticationRequired)?;
        let mut tokens: Tokens =
            serde_json::from_slice(&bytes).map_err(|_| GarminError::AuthenticationRequired)?;
        if tokens.di_token.is_empty() || tokens.di_client_id.is_empty() {
            return Err(GarminError::AuthenticationRequired);
        }
        let mut rotated = policy == RestorePolicy::Refresh || tokens.expires_soon();
        if rotated {
            tokens = self.refresh(schedule, tokens).await?;
        }
        let profile = match self.profile(schedule, &tokens).await {
            Err(GarminError::Unauthorized) if !rotated => {
                tokens = self.refresh(schedule, tokens).await?;
                rotated = true;
                self.profile(schedule, &tokens).await?
            }
            outcome => outcome?,
        };
        Ok((LegacySession { profile, tokens }, rotated))
    }

    async fn refresh(
        &self,
        schedule: &mut Schedule,
        tokens: Tokens,
    ) -> Result<Tokens, GarminError> {
        let refresh = tokens
            .di_refresh_token
            .filter(|value| !value.is_empty())
            .ok_or(GarminError::AuthenticationRequired)?;
        let grant = self
            .grant(
                schedule,
                &tokens.di_client_id,
                &[
                    ("grant_type", "refresh_token"),
                    ("client_id", &tokens.di_client_id),
                    ("refresh_token", &refresh),
                ],
            )
            .await
            .map_err(|error| match error {
                GarminError::Unauthorized
                | GarminError::Forbidden
                | GarminError::UnexpectedStatus(400) => GarminError::AuthenticationRequired,
                error => error,
            })?;
        Tokens::from_grant(grant, Some(refresh), &tokens.di_client_id)
    }

    async fn grant(
        &self,
        schedule: &mut Schedule,
        client: &str,
        fields: &[(&str, &str)],
    ) -> Result<TokenGrant, GarminError> {
        let basic = sensitive_header("Basic", &STANDARD.encode(format!("{client}:")))?;
        let request = native_headers(self.http.post(endpoint(&self.hosts.di, DI_PATH)?))
            .header(AUTHORIZATION, basic)
            .header(ACCEPT, "application/json")
            .header(CACHE_CONTROL, "no-cache")
            .form(fields);
        decode_json(schedule.send(self.pacing, request).await?, MAX_AUTH_BYTES).await
    }

    async fn profile(
        &self,
        schedule: &mut Schedule,
        tokens: &Tokens,
    ) -> Result<Profile, GarminError> {
        let bearer = sensitive_header("Bearer", &tokens.di_token)?;
        let request = native_headers(self.http.get(endpoint(&self.hosts.api, PROFILE_PATH)?))
            .header(AUTHORIZATION, bearer)
            .header(ACCEPT, "application/json");
        let response = successful(schedule.send(self.pacing, request).await?)?;
        let raw = read_bounded(response, MAX_PROFILE_BYTES).await?;
        let id = GarminProfileId::from_profile_json(&raw, MAX_PROFILE_BYTES as usize)
            .map_err(|_| GarminError::ProfileUnavailable)?;
        let value: serde_json::Value =
            serde_json::from_slice(&raw).map_err(|_| GarminError::ProfileUnavailable)?;
        let display_name = value
            .get("displayName")
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned);
        Ok(Profile { id, display_name })
    }
}

pub(super) async fn fetch_json<T: serde::de::DeserializeOwned>(
    request: RequestBuilder,
    limit: u64,
) -> Result<T, GarminError> {
    decode_json(send(request).await?, limit).await
}

async fn decode_json<T: serde::de::DeserializeOwned>(
    response: Response,
    limit: u64,
) -> Result<T, GarminError> {
    let response = successful(response)?;
    let body = read_bounded(response, limit).await?;
    serde_json::from_slice(&body).map_err(|_| GarminError::InvalidJson)
}
