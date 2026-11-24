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
    Database, WorkTopic,
    jobs::{DailyBackupLease, JobStoreError},
    runtime_lease::RuntimeWorkerIdentity,
};
use ga_domain::timestamp::StoredInstant;
use tokio::time::{Instant, MissedTickBehavior, interval_at, sleep};

use super::clock;
use crate::{backup, backup_space, configuration};

const PLAN_INTERVAL: Duration = Duration::from_secs(30);
const WORK_INTERVAL: Duration = Duration::from_secs(1);
const LEASE_SECONDS: u32 = 300;

async fn keep_lease(
    duration: Duration,
    mut renew: impl AsyncFnMut(StoredInstant) -> Result<bool, JobStoreError>,
) -> Result<(), ScheduledBackupError> {
    let tick = duration / 3;
    let mut timer = interval_at(Instant::now() + tick, tick);
    timer.set_missed_tick_behavior(MissedTickBehavior::Delay);
    loop {
        timer.tick().await;
        let now = clock().map_err(|_| ScheduledBackupError::Clock)?;
        if !renew(now).await? {
            return Err(JobStoreError::LeaseLost.into());
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{env, str::FromStr};

    use ga_archive::backup_space::BackupSpace;
    use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

    use super::*;

    #[tokio::test]
    #[ignore = "tests an expiring capacity lease on disposable TimescaleDB"]
    async fn renews_storage_check_while_capacity_estimate_waits() -> Result<(), Box<dyn Error>> {
        let url = env::var("GA_TEST_DATABASE_URL")?;
        let address = url
            .strip_prefix("postgresql+psycopg://")
            .ok_or("Expected fixture URL")?;
        let options = PgConnectOptions::from_str(&format!("postgresql://{address}"))?
            .disable_statement_logging();
        let target = options
            .get_database()
            .filter(|name| name.ends_with("_test"))
            .ok_or("Expected disposable database")?;
        let pool = PgPool::connect_with(options.clone()).await?;
        let actual: String = sqlx::query_scalar("SELECT current_database()")
            .fetch_one(&pool)
            .await?;
        assert_eq!(actual, target);
        let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
        let mut runtime = database.acquire_runtime_lease().await?;
        let worker = runtime.worker_identity().await?;
        let at = clock().map_err(|_| "Fixture clock unavailable")?;
        database.schedule_storage_check(at).await?;
        let lease = database
            .claim_storage_check(at, 1)
            .await?
            .ok_or("Expected due storage check")?;
        let renewal = keep_lease(lease.lease_duration(), |now| {
            database.renew_storage_check(&lease, now)
        });
        let estimate = async {
            sleep(Duration::from_millis(1600)).await;
            database
                .complete_storage_check(&lease, BackupSpace { volumes: vec![] }, at, worker, false)
                .await
        };
        tokio::select! {
            result = estimate => result?,
            failure = renewal => failure?,
        }
        let saved: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='storage:backup-capacity' AND value->>'status'='ready')
                AND EXISTS(SELECT 1 FROM jobs WHERE kind='storage_check' AND status='done' AND attempts=1)",
        ).fetch_one(&pool).await?;
        assert!(saved);
        assert!(!database.renew_storage_check(&lease, at).await?);
        Ok(())
    }
}

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

    pub(super) async fn run(
        &self,
        database: &Database,
        worker: RuntimeWorkerIdentity,
        notify_telegram: bool,
    ) -> ! {
        let mut next_plan = Instant::now();
        let mut work = database.work_subscription(WorkTopic::Maintenance);
        loop {
            let Ok(now) = clock() else {
                sleep(PLAN_INTERVAL).await;
                continue;
            };
            let mut failed = false;
            let mut handled = false;
            if Instant::now() >= next_plan {
                next_plan = Instant::now() + PLAN_INTERVAL;
                if let Err(error) = database.schedule_daily_backup(now).await {
                    eprintln!("{error}");
                    failed = true;
                }
                if let Err(error) = database.schedule_storage_check(now).await {
                    eprintln!("{error}");
                    failed = true;
                }
            }
            match database.claim_daily_backup(now, LEASE_SECONDS).await {
                Ok(Some(lease)) => {
                    handled = true;
                    if let Err(error) = self.with_renewal(database, worker, &lease).await {
                        eprintln!("{error}");
                        failed = true;
                        if let Ok(now) = clock() {
                            let _ = database.defer_failed_daily_backup(&lease, now).await;
                        }
                    }
                }
                Ok(None) => {}
                Err(error) => {
                    eprintln!("{error}");
                    failed = true;
                }
            }
            match self.check_capacity(database, worker, notify_telegram).await {
                Ok(checked) => handled |= checked,
                Err(error) => {
                    eprintln!("{error}");
                    failed = true;
                }
            }
            if failed || handled {
                // Preserve the existing pacing for actual maintenance work and
                // retries; only an empty queue waits for a signal or deadline.
                sleep(WORK_INTERVAL).await;
            } else {
                work.wait(next_plan.saturating_duration_since(Instant::now()))
                    .await;
            }
        }
    }

    async fn check_capacity(
        &self,
        database: &Database,
        worker: RuntimeWorkerIdentity,
        notify_telegram: bool,
    ) -> Result<bool, ScheduledBackupError> {
        let at = clock().map_err(|_| ScheduledBackupError::Clock)?;
        let Some(lease) = database.claim_storage_check(at, LEASE_SECONDS).await? else {
            return Ok(false);
        };
        let cancellation = CancelBackup(Arc::new(AtomicBool::new(false)));
        let cancelled = Arc::clone(&cancellation.0);
        let estimate = backup_space::estimate(
            database,
            self.data.clone(),
            self.tokens.clone(),
            self.directory.join("capacity-check.enc"),
            move || cancelled.load(Ordering::Acquire),
        );
        let renew = keep_lease(lease.lease_duration(), |at| {
            database.renew_storage_check(&lease, at)
        });
        let result = tokio::select! {
            biased;
            result=estimate=>result,
            expired=renew=>return expired.map(|()| true),
        };
        match result {
            Ok(estimate) => {
                database
                    .complete_storage_check(&lease, estimate, at, worker, notify_telegram)
                    .await?;
            }
            Err(error) => {
                let now = clock().map_err(|_| ScheduledBackupError::Clock)?;
                database.defer_storage_check(&lease, now).await?;
                return Err(ScheduledBackupError::Capacity(error));
            }
        }
        Ok(true)
    }

    async fn with_renewal(
        &self,
        database: &Database,
        worker: RuntimeWorkerIdentity,
        lease: &DailyBackupLease,
    ) -> Result<(), ScheduledBackupError> {
        let cancellation = CancelBackup(Arc::new(AtomicBool::new(false)));
        let work = self.write(database, worker, lease, &cancellation.0);
        let renew = keep_lease(lease.lease_duration(), |at| {
            database.renew_daily_backup(lease, at)
        });
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
