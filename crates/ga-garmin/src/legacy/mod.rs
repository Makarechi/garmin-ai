mod login;
mod query;
mod reader;
mod session;

#[cfg(test)]
mod tests;

use reqwest::{RequestBuilder, Url};

use crate::GarminError;

pub use login::{LegacyLogin, LoginProgress, MfaChallenge};
pub use query::{
    ActivityEndpoint, DailyEndpoint, LEGACY_CAPABILITIES, LegacyInventoryEntry, LegacyRead,
    LegacyResource,
};
pub use reader::LegacyReader;
pub use session::{LegacySession, RestorePolicy};

const SSO_ORIGIN: &str = "https://sso.garmin.com/";
const DI_ORIGIN: &str = "https://diauth.garmin.com/";
const API_ORIGIN: &str = "https://connectapi.garmin.com/";
const DI_PATH: &str = "di-oauth2-service/oauth/token";
const PROFILE_PATH: &str = "userprofile-service/socialProfile";
const MAX_AUTH_BYTES: u64 = 64 * 1024;
const MAX_PROFILE_BYTES: usize = 1024 * 1024;

#[derive(Clone)]
struct Hosts {
    sso: Url,
    di: Url,
    api: Url,
}

impl Hosts {
    fn production() -> Result<Self, GarminError> {
        let parse = |origin| Url::parse(origin).map_err(|_| GarminError::InvalidConfiguration);
        Ok(Self {
            sso: parse(SSO_ORIGIN)?,
            di: parse(DI_ORIGIN)?,
            api: parse(API_ORIGIN)?,
        })
    }
}

fn endpoint(base: &Url, path: &str) -> Result<Url, GarminError> {
    base.join(path)
        .map_err(|_| GarminError::InvalidConfiguration)
}

fn native_headers(request: RequestBuilder) -> RequestBuilder {
    request
        .header("User-Agent", "GCM-Android-5.23")
        .header("X-Garmin-User-Agent", "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; Android/33; Dalvik/2.1.0")
        .header("X-Garmin-Paired-App-Version", "10861")
        .header("X-Garmin-Client-Platform", "Android")
        .header("X-App-Ver", "10861")
        .header("X-Lang", "en")
        .header("X-GCExperience", "GC5")
        .header("Accept-Language", "en-US,en;q=0.9")
}
