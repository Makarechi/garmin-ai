mod init_data;

use std::{error::Error, fmt, path::PathBuf, sync::Arc, time::Duration};

use ga_archive::{
    GarminTokenFile,
    storage_files::{StorageAccess, StorageFilesGuard},
};
use ga_db::{
    Database,
    garmin_integration::{GarminReauthError, GarminReauthStatus, GarminReauthTarget},
};
use ga_garmin::{
    GarminError, TransportConfig,
    legacy::{LegacyLogin, LegacySession, LoginProgress, MfaChallenge},
    managed_password::GarminPasswordSecret,
};
use subtle::ConstantTimeEq;
use time::OffsetDateTime;
use tokio::{
    sync::Mutex,
    time::{Instant, sleep_until},
};
use zeroize::Zeroizing;

use crate::telegram_settings::TelegramIngressSettings;
use init_data::{TelegramSessionStage, validate_init_data};

const LOGIN_LIFETIME: Duration = Duration::from_secs(300);
const LOGIN_COOLDOWN: Duration = Duration::from_secs(60);
const MFA_ATTEMPTS: u8 = 3;

pub struct GarminMiniAppSettings {
    pub telegram: TelegramIngressSettings,
    pub bot_token: String,
    pub garmin_instance: String,
    pub email: Option<String>,
    pub password: Option<GarminPasswordSecret>,
    pub token_directory: PathBuf,
    pub lock_directory: PathBuf,
}

pub struct GarminMiniApp {
    database: Database,
    telegram: TelegramIngressSettings,
    bot_token: Zeroizing<String>,
    garmin_instance: String,
    email: Option<String>,
    password: Option<GarminPasswordSecret>,
    token_directory: PathBuf,
    lock_directory: PathBuf,
    state: Arc<Mutex<State>>,
}

#[derive(Default)]
struct State {
    last_start: Option<Instant>,
    session: Option<Session>,
}

struct Session {
    digest: [u8; 32],
    expires: Instant,
    attempts: u8,
    progress: Progress,
}

enum Progress {
    Challenge(Box<MfaChallenge>),
    Authenticated(Box<LegacySession>),
}

#[derive(Debug)]
pub enum GarminMiniAppError {
    Session,
    Configuration,
    Unavailable,
    TooSoon,
    Code,
    Database(GarminReauthError),
    Provider(GarminError),
    Password,
}

impl fmt::Display for GarminMiniAppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Session => f.write_str("Telegram Mini App session is invalid or expired"),
            Self::Configuration => f.write_str("Garmin web login is not configured"),
            Self::Unavailable => f.write_str("Garmin web reauthentication is unavailable"),
            Self::TooSoon => f.write_str("Garmin login was started recently"),
            Self::Code => f.write_str("Garmin code is invalid or expired"),
            Self::Database(error) => error.fmt(f),
            Self::Provider(error) => error.fmt(f),
            Self::Password => f.write_str("The configured Garmin password secret is unavailable"),
        }
    }
}
impl Error for GarminMiniAppError {}
impl From<GarminReauthError> for GarminMiniAppError {
    fn from(error: GarminReauthError) -> Self {
        Self::Database(error)
    }
}
impl From<GarminError> for GarminMiniAppError {
    fn from(error: GarminError) -> Self {
        Self::Provider(error)
    }
}

#[derive(Clone, Copy)]
pub enum GarminMiniAppResult {
    AlreadyConnected,
    CodeRequired,
    Restored,
}

impl GarminMiniAppResult {
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::AlreadyConnected => "already_connected",
            Self::CodeRequired => "code_required",
            Self::Restored => "restored",
        }
    }
}

impl GarminMiniApp {
    #[must_use]
    pub fn new(database: Database, settings: GarminMiniAppSettings) -> Self {
        Self {
            database,
            telegram: settings.telegram,
            bot_token: Zeroizing::new(settings.bot_token),
            garmin_instance: settings.garmin_instance,
            email: settings.email,
            password: settings.password,
            token_directory: settings.token_directory,
            lock_directory: settings.lock_directory,
            state: Arc::new(Mutex::new(State::default())),
        }
    }

    pub async fn start(&self, init_data: &str) -> Result<GarminMiniAppResult, GarminMiniAppError> {
        let digest = self.authenticate(init_data, TelegramSessionStage::Start)?;
        let mut state = self.state.lock().await;
        match self
            .database
            .garmin_web_reauth_status(self.target())
            .await?
        {
            GarminReauthStatus::Active => return Ok(GarminMiniAppResult::AlreadyConnected),
            GarminReauthStatus::Unavailable => return Err(GarminMiniAppError::Unavailable),
            GarminReauthStatus::Required => {}
        }
        let email = self
            .email
            .as_deref()
            .ok_or(GarminMiniAppError::Configuration)?;
        let password = self
            .password
            .as_ref()
            .ok_or(GarminMiniAppError::Configuration)?;
        let now = Instant::now();
        if state
            .last_start
            .is_some_and(|last| now.duration_since(last) < LOGIN_COOLDOWN)
        {
            return Err(GarminMiniAppError::TooSoon);
        }
        state.last_start = Some(now);
        state.session = None;
        let password = password
            .read()
            .await
            .map_err(|_| GarminMiniAppError::Password)?;
        let progress = LegacyLogin::new(TransportConfig::default())?
            .start(email, &password)
            .await?;
        drop(password);
        match progress {
            LoginProgress::Complete(session) => {
                self.publish(&session).await?;
                Ok(GarminMiniAppResult::Restored)
            }
            LoginProgress::Mfa(challenge) => {
                let expires = Instant::now() + LOGIN_LIFETIME;
                state.session = Some(Session {
                    digest,
                    expires,
                    attempts: 0,
                    progress: Progress::Challenge(challenge),
                });
                let state = Arc::downgrade(&self.state);
                tokio::spawn(async move {
                    sleep_until(expires).await;
                    if let Some(state) = state.upgrade() {
                        let mut state = state.lock().await;
                        if state
                            .session
                            .as_ref()
                            .is_some_and(|current| current.expires == expires)
                        {
                            state.session = None;
                        }
                    }
                });
                Ok(GarminMiniAppResult::CodeRequired)
            }
        }
    }

    pub async fn complete(
        &self,
        init_data: &str,
        code: &str,
    ) -> Result<GarminMiniAppResult, GarminMiniAppError> {
        let digest = self.authenticate(init_data, TelegramSessionStage::Complete)?;
        let mut state = self.state.lock().await;
        if self
            .database
            .garmin_web_reauth_status(self.target())
            .await?
            != GarminReauthStatus::Required
        {
            return Err(GarminMiniAppError::Unavailable);
        }
        if !(1..=20).contains(&code.len()) || !code.bytes().all(|byte| byte.is_ascii_alphanumeric())
        {
            return Err(GarminMiniAppError::Code);
        }
        let session = match state.session.as_mut() {
            Some(session) if Instant::now() < session.expires => session,
            _ => {
                state.session = None;
                return Err(GarminMiniAppError::Code);
            }
        };
        if !bool::from(session.digest.ct_eq(&digest)) {
            return Err(GarminMiniAppError::Code);
        }
        if let Progress::Challenge(challenge) = &mut session.progress {
            session.attempts += 1;
            match challenge.verify(code).await {
                Ok(verified) => session.progress = Progress::Authenticated(Box::new(verified)),
                Err(error) => {
                    if error == GarminError::MfaExpired || session.attempts >= MFA_ATTEMPTS {
                        state.session = None;
                    }
                    return Err(match error {
                        GarminError::MfaExpired | GarminError::InvalidMfaCode => {
                            GarminMiniAppError::Code
                        }
                        other => other.into(),
                    });
                }
            }
        }
        let Progress::Authenticated(verified) = &session.progress else {
            return Err(GarminMiniAppError::Code);
        };
        self.publish(verified).await?;
        state.session = None;
        Ok(GarminMiniAppResult::Restored)
    }

    fn authenticate(
        &self,
        init_data: &str,
        stage: TelegramSessionStage,
    ) -> Result<[u8; 32], GarminMiniAppError> {
        validate_init_data(
            init_data,
            &self.bot_token,
            self.telegram.external_owner_id(),
            OffsetDateTime::now_utc().unix_timestamp(),
            stage,
        )
        .map_err(|_| GarminMiniAppError::Session)
    }

    fn target(&self) -> GarminReauthTarget<'_> {
        GarminReauthTarget {
            telegram_instance: self.telegram.instance_id(),
            telegram_owner: self.telegram.external_owner_id(),
            garmin_instance: &self.garmin_instance,
        }
    }

    async fn publish(&self, session: &LegacySession) -> Result<(), GarminMiniAppError> {
        let fingerprint = session.fingerprint();
        self.database
            .commit_garmin_web_reauth(self.target(), &fingerprint, || {
                let Ok(_files) =
                    StorageFilesGuard::acquire(&self.lock_directory, StorageAccess::Active)
                else {
                    return false;
                };
                let Ok(tokens) = GarminTokenFile::open(&self.token_directory) else {
                    return false;
                };
                session.save_for_owner(&tokens, &fingerprint).is_ok()
            })
            .await?;
        Ok(())
    }
}
