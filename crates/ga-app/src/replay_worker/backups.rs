use std::{
    error::Error,
    fmt, fs, io,
    num::NonZeroU16,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use ga_archive::scheduled_backup::confirm_daily_backup;
use ga_db::{
    Database,
    jobs::{DailyBackupLease, JobStoreError},
    runtime_lease::RuntimeWorkerIdentity,
};
use ga_domain::timestamp::StoredInstant;
use tokio::time::{Instant, MissedTickBehavior, interval_at, sleep};

use super::clock;
use crate::{backup, backup_space, configuration};

const PLAN_INTERVAL: Duration = Duration::from_secs(30);
const POLL_INTERVAL: Duration = Duration::from_secs(1);
const LEASE_SECONDS: u32 = 300;

pub(super) struct ScheduledBackups {
    directory: PathBuf,
    key: [u8; 32],
    keep: NonZeroU16,
    data: PathBuf,
    tokens: PathBuf,
}

#[derive(Debug)]
pub(super) enum ScheduledBackupError {
    Configuration,
    File,
    Clock,
    Job(JobStoreError),
    Capacity(&'static str),
}

impl fmt::Display for ScheduledBackupError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration => f.write_str("Scheduled backup configuration is invalid"),
            Self::File => f.write_str("Scheduled backup file could not be created or verified"),
            Self::Clock => f.write_str("Scheduled backup time is unavailable"),
            Self::Job(error) => error.fmt(f),
            Self::Capacity(error) => f.write_str(error),
        }
    }
}
impl Error for ScheduledBackupError {}
impl From<JobStoreError> for ScheduledBackupError {
    fn from(error: JobStoreError) -> Self {
        Self::Job(error)
    }
}

impl ScheduledBackups {
    pub(super) fn read() -> Result<Option<Self>, ScheduledBackupError> {
        let [key, directory, keep, data, tokens] = configuration::read([
            "GA_BACKUP_KEY",
            "GA_BACKUP_DIR",
            "GA_BACKUP_KEEP_DAILY",
            "GA_DATA_DIR",
            "GA_TOKEN_DIR",
        ])
        .map_err(|_| ScheduledBackupError::Configuration)?;
        let Some(encoded) = key.filter(|value| !value.is_empty()) else {
            return Ok(None);
        };
        let key = backup::decode_key(&encoded).map_err(|_| ScheduledBackupError::Configuration)?;
        let keep = keep
            .as_deref()
            .unwrap_or("14")
            .parse::<NonZeroU16>()
            .ok()
            .filter(|keep| keep.get() <= 365)
            .ok_or(ScheduledBackupError::Configuration)?;
        let directory = PathBuf::from(directory.unwrap_or_else(|| "backups".into()));
        backup::validate_scheduled_destination(&directory)
            .map_err(|_| ScheduledBackupError::Configuration)?;
        let (data, tokens) = backup_space::paths(data, tokens);
        Ok(Some(Self {
            directory,
            key,
            keep,
            data,
            tokens,
        }))
    }

    pub(super) async fn run(&self, database: &Database, worker: RuntimeWorkerIdentity) -> ! {
        let mut next_plan = Instant::now();
        loop {
            let Ok(now) = clock() else {
                sleep(PLAN_INTERVAL).await;
                continue;
            };
            if Instant::now() >= next_plan {
                next_plan = Instant::now() + PLAN_INTERVAL;
                if let Err(error) = database.schedule_daily_backup(now).await {
                    eprintln!("{error}");
                }
                if let Err(error) = database.schedule_storage_check(now).await {
                    eprintln!("{error}");
                }
            }
            match database.claim_daily_backup(now, LEASE_SECONDS).await {
                Ok(Some(lease)) => {
                    if let Err(error) = self.with_renewal(database, worker, &lease).await {
                        eprintln!("{error}");
                        if let Ok(now) = clock() {
                            let _ = database.defer_failed_daily_backup(&lease, now).await;
                        }
                    }
                }
                Ok(None) => {}
                Err(error) => eprintln!("{error}"),
            }
            if let Err(error) = self.check_capacity(database).await {
                eprintln!("{error}");
            }
            sleep(POLL_INTERVAL).await;
        }
    }

    async fn check_capacity(&self, database: &Database) -> Result<(), ScheduledBackupError> {
        let at = clock().map_err(|_| ScheduledBackupError::Clock)?;
        let Some(lease) = database.claim_storage_check(at, LEASE_SECONDS).await? else {
            return Ok(());
        };
        match backup_space::estimate(
            database,
            self.data.clone(),
            self.tokens.clone(),
            self.directory.join("capacity-check.enc"),
        )
        .await
        {
            Ok(estimate) => {
                database
                    .complete_storage_check(&lease, estimate, at)
                    .await?
            }
            Err(error) => {
                let now = clock().map_err(|_| ScheduledBackupError::Clock)?;
                database.defer_storage_check(&lease, now).await?;
                return Err(ScheduledBackupError::Capacity(error));
            }
        }
        Ok(())
    }

    async fn with_renewal(
        &self,
        database: &Database,
        worker: RuntimeWorkerIdentity,
        lease: &DailyBackupLease,
    ) -> Result<(), ScheduledBackupError> {
        let cancellation = CancelBackup(Arc::new(AtomicBool::new(false)));
        let work = self.write(database, worker, lease, &cancellation.0);
        let tick = lease.lease_duration() / 3;
        let renew = async {
            let mut timer = interval_at(Instant::now() + tick, tick);
            timer.set_missed_tick_behavior(MissedTickBehavior::Delay);
            loop {
                timer.tick().await;
                let now = clock().map_err(|_| ScheduledBackupError::Clock)?;
                if !database.renew_daily_backup(lease, now).await? {
                    return Err(JobStoreError::LeaseLost.into());
                }
            }
        };
        tokio::select! {
            biased;
            result=work=>result,
            expired=renew=>expired,
        }
    }

    async fn write(
        &self,
        database: &Database,
        worker: RuntimeWorkerIdentity,
        lease: &DailyBackupLease,
        cancelled: &Arc<AtomicBool>,
    ) -> Result<(), ScheduledBackupError> {
        let destination = self
            .directory
            .join(format!("garmin-ai-{}.enc", lease.date()));
        match fs::symlink_metadata(&destination) {
            Ok(existing) if existing.is_file() => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let path = destination.clone();
                let job = *lease;
                let cancelled = Arc::clone(cancelled);
                let written = tokio::task::spawn_blocking(move || {
                    backup::create_scheduled(&path, worker, job, || {
                        cancelled.load(Ordering::Acquire)
                    })
                    .map_err(|_| ScheduledBackupError::File)
                })
                .await
                .map_err(|_| ScheduledBackupError::File)?;
                if written.is_err() && !destination.exists() {
                    return written;
                }
            }
            _ => return Err(ScheduledBackupError::File),
        }
        let path = destination.clone();
        let key = self.key;
        let authentication_cancellation = Arc::clone(cancelled);
        let confirmed = tokio::task::spawn_blocking(move || {
            confirm_daily_backup(&path, &key, || {
                authentication_cancellation.load(Ordering::Acquire)
            })
        })
        .await
        .map_err(|_| ScheduledBackupError::File)?
        .map_err(|_| ScheduledBackupError::File)?;
        let now = clock().map_err(|_| ScheduledBackupError::Clock)?;
        let still_running = database
            .worker_identity_held(worker)
            .await
            .map_err(|_| ScheduledBackupError::Job(JobStoreError::LeaseLost))?;
        if !still_running || !database.renew_daily_backup(lease, now).await? {
            return Err(JobStoreError::LeaseLost.into());
        }
        let keep = self.keep;
        let cancellation = Arc::clone(cancelled);
        let created = tokio::task::spawn_blocking(move || {
            confirmed.prune(keep, || cancellation.load(Ordering::Acquire))
        })
        .await
        .map_err(|_| ScheduledBackupError::File)?
        .map_err(|_| ScheduledBackupError::File)?;
        let date = archive_time(created)?;
        let destination = destination
            .to_str()
            .ok_or(ScheduledBackupError::Configuration)?;
        database
            .complete_worker_backup(lease, destination, date, worker)
            .await?;
        Ok(())
    }
}

struct CancelBackup(Arc<AtomicBool>);

impl Drop for CancelBackup {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

fn archive_time(value: SystemTime) -> Result<StoredInstant, ScheduledBackupError> {
    let micros = value
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|value| i64::try_from(value.as_micros()).ok())
        .ok_or(ScheduledBackupError::Clock)?;
    StoredInstant::from_unix_microseconds(micros).map_err(|_| ScheduledBackupError::Clock)
}
