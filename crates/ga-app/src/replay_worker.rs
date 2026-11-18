use std::{
    env,
    error::Error,
    fmt, future,
    num::NonZeroUsize,
    path::PathBuf,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use ga_app::{
    model_gateway::{ModelEnvironmentError, RuntimeModelSettings},
    telegram_settings::TelegramIngressSettings,
    telegram_transport::{DiaryReminderSettings, TelegramTransport},
};
use ga_archive::Archive;
use ga_db::{
    Database,
    jobs::{JobStoreError, RecordedJobStatus},
    replay::{ReplaySelectionError, RunReplayJob},
    runtime_lease::RuntimeLeaseError,
};
use ga_domain::{
    event_input::known_timezone, event_payload::legacy_boolean, timestamp::StoredInstant,
};
use ga_fit::{ExportLimits, MAX_FIT_BYTES};
use time::Time;
use tokio::{signal, sync::watch, time::Instant};

use crate::configuration;

const WORKER_POLL: Duration = Duration::from_secs(1);
const PLAN_BUSY: Duration = Duration::from_secs(30);
const PLAN_IDLE: Duration = Duration::from_secs(15 * 60);
const LEASE_SECONDS: u32 = 120;
const MAX_JSON_BYTES: u64 = 8 * 1024 * 1024;
const MAX_ZIP_ENTRIES: NonZeroUsize = match NonZeroUsize::new(4096) {
    Some(limit) => limit,
    None => NonZeroUsize::MIN,
};

#[derive(Debug)]
pub enum ReplayServiceError {
    Configuration,
    Archive,
    TelegramConfiguration,
    Clock,
    Shutdown,
    Planning(ReplaySelectionError),
    Lease(RuntimeLeaseError),
    Jobs(JobStoreError),
}

impl fmt::Display for ReplayServiceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration => f.write_str("Archive replay configuration is invalid"),
            Self::Archive => f.write_str("Could not open the existing Garmin archive"),
            Self::TelegramConfiguration => f.write_str("Telegram worker configuration is invalid"),
            Self::Clock => f.write_str("Archive replay clock cannot be represented"),
            Self::Shutdown => f.write_str("Could not listen for a worker shutdown signal"),
            Self::Planning(error) => error.fmt(f),
            Self::Lease(error) => error.fmt(f),
            Self::Jobs(error) => error.fmt(f),
        }
    }
}
impl Error for ReplayServiceError {}

struct Configuration {
    database_url: String,
    raw_archive: PathBuf,
    timezone: String,
}

impl Configuration {
    fn read() -> Result<Self, ReplayServiceError> {
        let [url, path, timezone] =
            configuration::read(["GA_DATABASE_URL", "GA_DATA_DIR", "GA_TIMEZONE"])
                .map_err(|_| ReplayServiceError::Configuration)?;
        let database_url = url
            .filter(|url| !url.is_empty())
            .ok_or(ReplayServiceError::Configuration)?;
        let data_dir = PathBuf::from(path.unwrap_or_else(|| "data".into()));
        let raw_archive = std::path::absolute(data_dir)
            .map_err(|_| ReplayServiceError::Configuration)?
            .join("raw");
        let timezone = timezone.unwrap_or_else(|| "Europe/Bratislava".into());
        if !known_timezone(&timezone) {
            return Err(ReplayServiceError::Configuration);
        }
        Ok(Self {
            database_url,
            raw_archive,
            timezone,
        })
    }
}

struct TelegramWorker {
    settings: TelegramIngressSettings,
    transport: TelegramTransport,
    reminders: DiaryReminderSettings,
}

fn telegram_configuration(timezone: &str) -> Result<Option<TelegramWorker>, ReplayServiceError> {
    let failure = |_| ReplayServiceError::TelegramConfiguration;
    let [
        token,
        user,
        integrations,
        budget,
        enabled,
        quiet_start,
        quiet_end,
    ] = configuration::read([
        "GA_TELEGRAM_BOT_TOKEN",
        "GA_TELEGRAM_USER_ID",
        "GA_INTEGRATIONS",
        "GA_QUESTION_BUDGET",
        "GA_PROACTIVE_ENABLED",
        "GA_QUIET_START_HOUR",
        "GA_QUIET_END_HOUR",
    ])
    .map_err(failure)?;
    let Some(token) = token.filter(|token| !token.is_empty()) else {
        return Ok(None);
    };
    let Some(settings) =
        TelegramIngressSettings::selected(user.as_deref(), integrations.as_deref())
            .map_err(|_| ReplayServiceError::TelegramConfiguration)?
    else {
        return Ok(None);
    };
    let transport =
        TelegramTransport::new(&token).map_err(|_| ReplayServiceError::TelegramConfiguration)?;
    let question_budget = budget
        .as_deref()
        .unwrap_or("2")
        .parse::<u8>()
        .ok()
        .filter(|value| *value <= 20)
        .ok_or(ReplayServiceError::TelegramConfiguration)?;
    let enabled = enabled
        .map_or(Some(false), |value| {
            legacy_boolean(&serde_json::Value::String(value))
        })
        .ok_or(ReplayServiceError::TelegramConfiguration)?;
    let quiet = |hour: Option<&str>, fallback: &str| {
        hour.unwrap_or(fallback)
            .parse::<u8>()
            .ok()
            .and_then(|hour| Time::from_hms(hour, 0, 0).ok())
            .ok_or(ReplayServiceError::TelegramConfiguration)
    };
    Ok(Some(TelegramWorker {
        settings,
        transport,
        reminders: DiaryReminderSettings {
            timezone: timezone.to_owned(),
            enabled,
            daily_budget: question_budget,
            quiet_start: quiet(quiet_start.as_deref(), "22")?,
            quiet_end: quiet(quiet_end.as_deref(), "8")?,
        },
    }))
}

fn reminder_model_available(database: &Database, now: StoredInstant) -> bool {
    let settings = RuntimeModelSettings::resolve(
        |name| {
            configuration::read([name])
                .map(|[value]| value)
                .map_err(|_| ModelEnvironmentError::Invalid)
        },
        |name| match env::var(name) {
            Ok(value) => Ok(Some(value)),
            Err(env::VarError::NotPresent) => Ok(None),
            Err(env::VarError::NotUnicode(_)) => Err(ModelEnvironmentError::Invalid),
        },
    );
    match settings {
        Ok(Some(settings)) => settings
            .configured()
            .gateway(database.clone(), false, now)
            .is_ok(),
        Ok(None) => false,
        Err(error) => {
            eprintln!("{error}");
            false
        }
    }
}

#[tokio::main(flavor = "current_thread")]
pub async fn run(once: bool, worker: bool) -> Result<(), ReplayServiceError> {
    let config = Configuration::read()?;
    let telegram = worker
        .then(|| telegram_configuration(&config.timezone))
        .transpose()?
        .flatten();
    let database = Database::connect_lazy(&config.database_url)
        .map_err(|_| ReplayServiceError::Configuration)?;
    let reminders_available = telegram.is_some() && reminder_model_available(&database, clock()?);
    let archive = Arc::new(
        Archive::open_read_only(&config.raw_archive).map_err(|_| ReplayServiceError::Archive)?,
    );
    let mut singleton = database
        .acquire_runtime_lease()
        .await
        .map_err(ReplayServiceError::Lease)?;
    let timezone = singleton
        .owner_timezone()
        .await
        .map_err(ReplayServiceError::Lease)?
        .unwrap_or_else(|| config.timezone.clone());
    if worker {
        singleton
            .publish_worker_heartbeat()
            .await
            .map_err(ReplayServiceError::Lease)?;
    }
    #[cfg(unix)]
    let mut terminate = signal::unix::signal(signal::unix::SignalKind::terminate())
        .map_err(|_| ReplayServiceError::Shutdown)?;
    let shutdown = async move {
        #[cfg(unix)]
        tokio::select! {
            result = signal::ctrl_c() => result,
            _ = terminate.recv() => Ok(()),
        }
        #[cfg(not(unix))]
        signal::ctrl_c().await
    };
    println!(
        "{}",
        if worker {
            "Garmin AI worker ready"
        } else {
            "Archive replay worker ready"
        }
    );
    let (recovered, wait_for_recovery) = watch::channel(false);
    let receive = async {
        if let Some(telegram) = &telegram {
            if reminders_available {
                telegram
                    .transport
                    .run_receiver_with_reminders(
                        &database,
                        &mut singleton,
                        &telegram.settings,
                        recovered,
                    )
                    .await
            } else {
                telegram
                    .transport
                    .run_receiver(&database, &mut singleton, &telegram.settings)
                    .await
            }
            .map_err(ReplayServiceError::Lease)
        } else {
            singleton
                .while_held(future::pending::<()>())
                .await
                .map_err(ReplayServiceError::Lease)
        }
    };
    let telegram_work = async {
        if let Some(TelegramWorker {
            transport,
            settings,
            reminders,
        }) = &telegram
        {
            tokio::select! {
                result = transport.run_acknowledgements(&database, settings) => result,
                result = transport.run_owner_controls(&database, settings, u16::from(reminders.daily_budget), &config.timezone) => result,
                result = transport.run_replies(&database, settings) => result,
                result = async {
                    if reminders_available {
                        transport.run_diary_reminders(&database, settings, reminders, wait_for_recovery).await
                    }
                    future::pending().await
                } => result,
            }
        }
        future::pending::<Result<(), ReplayServiceError>>().await
    };
    tokio::select! {
        biased;
        result = shutdown => result.map_err(|_| ReplayServiceError::Shutdown),
        result = receive => result,
        result = telegram_work => result,
        result = process_archive(&database, archive, &timezone, once) => result,
    }
}

async fn process_archive(
    database: &Database,
    archive: Arc<Archive>,
    timezone: &str,
    once: bool,
) -> Result<(), ReplayServiceError> {
    let limits = ExportLimits {
        max_bytes: MAX_FIT_BYTES,
        max_zip_entries: MAX_ZIP_ENTRIES,
        max_duration: Duration::from_secs(120),
    };
    let mut next_plan = Instant::now();
    loop {
        if Instant::now() >= next_plan {
            let plan = database
                .schedule_bound_replay(clock()?)
                .await
                .map_err(ReplayServiceError::Planning)?;
            next_plan = Instant::now()
                + if plan.plan_again_soon() {
                    PLAN_BUSY
                } else {
                    PLAN_IDLE
                };
        }
        let lease = database
            .claim_replay_job(clock()?, LEASE_SECONDS)
            .await
            .map_err(ReplayServiceError::Jobs)?;
        if let Some(lease) = lease {
            let outcome = database
                .run_replay_job(
                    RunReplayJob {
                        lease: &lease,
                        configured_timezone: timezone,
                        max_json_bytes: MAX_JSON_BYTES,
                        fit_limits: limits,
                    },
                    archive.clone(),
                    || false,
                )
                .await;
            let status = match outcome {
                Ok(result) => Some(result.job),
                Err(_) => match database.fail_replay_job(&lease).await {
                    Ok(status) => Some(status),
                    Err(JobStoreError::LeaseLost) => None,
                    Err(error) => return Err(ReplayServiceError::Jobs(error)),
                },
            };
            println!(
                "{}",
                match status {
                    Some(RecordedJobStatus::Completed) => "Archive replay completed",
                    Some(RecordedJobStatus::Deferred) => "Archive replay scheduled to retry",
                    Some(RecordedJobStatus::Failed) => "Archive replay failed",
                    None => "Archive replay claim expired",
                }
            );
        } else if once {
            println!("No archive replay is due");
        } else {
            tokio::time::sleep(WORKER_POLL).await;
        }
        if once {
            return Ok(());
        }
    }
}

fn clock() -> Result<StoredInstant, ReplayServiceError> {
    let micros = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|time| i64::try_from(time.as_micros()).ok())
        .ok_or(ReplayServiceError::Clock)?;
    StoredInstant::from_unix_microseconds(micros).map_err(|_| ReplayServiceError::Clock)
}
