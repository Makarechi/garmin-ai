use std::time::Duration;

use reqwest::{
    Client, RequestBuilder,
    header::{ACCEPT, ORIGIN, USER_AGENT},
};
use serde::Deserialize;
use serde_json::json;
use tokio::time::Instant;

use super::session::{LegacySession, Native, fetch_json};
use super::{Hosts, MAX_AUTH_BYTES, endpoint};
use crate::{GarminError, TransportConfig, build_http};

const IOS_SERVICE: &str = "https://mobile.integration.garmin.com/gcm/ios";
const IOS_CLIENT: &str = "GCM_IOS_DARK";
const IOS_USER_AGENT: &str = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148";
const MFA_ATTEMPTS: u8 = 3;
const MFA_WINDOW: Duration = Duration::from_secs(300);

pub struct LegacyLogin {
    sso_http: Client,
    hosts: Hosts,
    native: Native,
}

pub enum LoginProgress {
    Complete(LegacySession),
    Mfa(Box<MfaChallenge>),
}

pub struct MfaChallenge {
    login: LegacyLogin,
    method: String,
    deadline: Instant,
    attempts: u8,
    used: bool,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct SsoResponse {
    response_status: Option<SsoStatus>,
    service_ticket_id: Option<String>,
    customer_mfa_info: Option<MfaInfo>,
    error: Option<SsoError>,
}

#[derive(Deserialize)]
struct SsoStatus {
    #[serde(rename = "type")]
    kind: String,
}

#[derive(Deserialize)]
struct MfaInfo {
    #[serde(rename = "mfaLastMethodUsed")]
    method: Option<String>,
}

#[derive(Deserialize)]
struct SsoError {
    #[serde(rename = "status-code")]
    status: serde_json::Value,
}

impl LegacyLogin {
    pub fn new(config: TransportConfig) -> Result<Self, GarminError> {
        Self::with_hosts(Hosts::production()?, config)
    }

    pub(super) fn with_hosts(hosts: Hosts, config: TransportConfig) -> Result<Self, GarminError> {
        let sso_http = build_http(config, hosts.sso.scheme() == "https")?
            .cookie_store(true)
            .build()
            .map_err(|_| GarminError::InvalidConfiguration)?;
        let native = Native::new(hosts.clone(), config)?;
        Ok(Self {
            sso_http,
            hosts,
            native,
        })
    }

    pub async fn start(self, username: &str, password: &str) -> Result<LoginProgress, GarminError> {
        if username.is_empty()
            || password.is_empty()
            || username.len() > 1024
            || password.len() > 1024
        {
            return Err(GarminError::InvalidRequest);
        }
        let request = self.sso_request("mobile/api/login")?.json(&json!({
            "username": username,
            "password": password,
            "rememberMe": true,
            "captchaToken": "",
        }));
        let reply: SsoResponse = fetch_json(request, MAX_AUTH_BYTES).await.map_err(|error| {
            if error == GarminError::Unauthorized {
                GarminError::AuthenticationRequired
            } else {
                error
            }
        })?;
        match reply.status()? {
            "MFA_REQUIRED" => {
                let method = reply
                    .customer_mfa_info
                    .and_then(|details| details.method)
                    .unwrap_or_else(|| "email".to_owned());
                Ok(LoginProgress::Mfa(Box::new(MfaChallenge {
                    login: self,
                    method,
                    deadline: Instant::now() + MFA_WINDOW,
                    attempts: 0,
                    used: false,
                })))
            }
            "SUCCESSFUL" => Ok(LoginProgress::Complete(self.finish(reply).await?)),
            "INVALID_USERNAME_PASSWORD" => Err(GarminError::AuthenticationRequired),
            "CAPTCHA_REQUIRED" => Err(GarminError::Forbidden),
            _ => Err(GarminError::InvalidTokenResponse),
        }
    }

    async fn finish(&self, reply: SsoResponse) -> Result<LegacySession, GarminError> {
        let ticket = reply
            .service_ticket_id
            .as_deref()
            .filter(|value| !value.is_empty())
            .ok_or(GarminError::InvalidTokenResponse)?;
        self.native.login(ticket, IOS_SERVICE).await
    }

    fn sso_request(&self, path: &str) -> Result<RequestBuilder, GarminError> {
        let url = endpoint(&self.hosts.sso, path)?;
        Ok(self
            .sso_http
            .post(url)
            .header(USER_AGENT, IOS_USER_AGENT)
            .header(ACCEPT, "application/json, text/plain, */*")
            .header(ORIGIN, self.hosts.sso.origin().ascii_serialization())
            .query(&[
                ("clientId", IOS_CLIENT),
                ("locale", "en-US"),
                ("service", IOS_SERVICE),
            ]))
    }
}

impl SsoResponse {
    fn status(&self) -> Result<&str, GarminError> {
        if self
            .error
            .as_ref()
            .is_some_and(|error| error.status == 429 || error.status == "429")
        {
            return Err(GarminError::RateLimited {
                retry_after: Duration::from_secs(900),
            });
        }
        self.response_status
            .as_ref()
            .map(|status| status.kind.as_str())
            .ok_or(GarminError::InvalidTokenResponse)
    }
}

impl MfaChallenge {
    pub async fn verify(&mut self, code: &str) -> Result<LegacySession, GarminError> {
        if self.used || self.attempts >= MFA_ATTEMPTS || Instant::now() >= self.deadline {
            return Err(GarminError::MfaExpired);
        }
        if code.is_empty()
            || code.len() > 20
            || !code.bytes().all(|byte| byte.is_ascii_alphanumeric())
        {
            return Err(GarminError::InvalidMfaCode);
        }
        self.attempts += 1;
        let request = self
            .login
            .sso_request("mobile/api/mfa/verifyCode")?
            .json(&json!({
                "mfaMethod": self.method,
                "mfaVerificationCode": code,
                "rememberMyBrowser": true,
                "reconsentList": [],
                "mfaSetup": false,
            }));
        let response: SsoResponse = fetch_json(request, MAX_AUTH_BYTES).await.map_err(|error| {
            if error == GarminError::Unauthorized {
                GarminError::InvalidMfaCode
            } else {
                error
            }
        })?;
        if response.status()? != "SUCCESSFUL" {
            return Err(GarminError::InvalidMfaCode);
        }
        self.used = true;
        self.login.finish(response).await
    }
}
