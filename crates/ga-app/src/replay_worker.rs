use std::{
    env,
    error::Error,
    fmt, io,
    num::NonZeroUsize,
    path::PathBuf,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use ga_archive::Archive;
use ga_db::{
    Database,
    jobs::{JobStoreError, RecordedJobStatus},
    replay::{ReplaySelectionError, RunReplayJob},
    runtime_lease::{RuntimeLease, RuntimeLeaseError},
};
use ga_domain::{event_input::known_timezone, timestamp::StoredInstant};
use ga_fit::{ExportLimits, MAX_FIT_BYTES};
use tokio::{signal, time::Instant};

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
        const KEYS: [&str; 3] = ["GA_DATABASE_URL", "GA_DATA_DIR", "GA_TIMEZONE"];
        let mut values = [None, None, None];
        match dotenvy::from_path_iter(".env") {
            Ok(entries) => {
                for (name, value) in entries.filter_map(Result::ok) {
                    if let Some(index) = KEYS.iter().position(|key| name.eq_ignore_ascii_case(key))
                    {
                        values[index] = Some(value);
                    }
                }
            }
            Err(dotenvy::Error::Io(error)) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(ReplayServiceError::Configuration),
        }
        for (name, value) in env::vars_os() {
            if let Some(index) = name
                .to_str()
                .and_then(|name| KEYS.iter().position(|key| name.eq_ignore_ascii_case(key)))
            {
                values[index] = Some(
                    value
                        .into_string()
                        .map_err(|_| ReplayServiceError::Configuration)?,
                );
            }
        }
        let [url, path, timezone] = values;
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

#[tokio::main(flavor = "current_thread")]
pub async fn run(once: bool) -> Result<(), ReplayServiceError> {
    let config = Configuration::read()?;
    let database = Database::connect_lazy(&config.database_url)
        .map_err(|_| ReplayServiceError::Configuration)?;
    let archive = Arc::new(
        Archive::open_read_only(&config.raw_archive).map_err(|_| ReplayServiceError::Archive)?,
    );
    let mut singleton = database
        .acquire_runtime_lease()
        .await
        .map_err(ReplayServiceError::Lease)?;
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
    println!("Archive replay worker ready");
    tokio::select! {
        biased;
        result = shutdown => result.map_err(|_| ReplayServiceError::Shutdown),
        result = verify_runtime(&mut singleton) => result,
        result = process_archive(&database, archive, &config.timezone, once) => result,
    }
}

async fn verify_runtime(singleton: &mut RuntimeLease) -> Result<(), ReplayServiceError> {
    loop {
        tokio::time::sleep(PLAN_BUSY).await;
        singleton.check().await.map_err(ReplayServiceError::Lease)?;
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
