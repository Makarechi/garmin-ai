use std::{error::Error, fmt, num::NonZeroUsize, path::Path, sync::Arc, time::Duration};

use ga_app::garmin_worker::{
    EndpointWorker, GarminConfigurationError, GarminQueueStep, GarminSourceSettings,
    ScheduledGarminWorker,
};
use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    garmin_integration::{GarminSourceAdmission, GarminTransportOutcome},
    garmin_periodic::GarminSyncSchedule,
};
use ga_fit::{ExportLimits, MAX_FIT_BYTES};
use ga_garmin::{GarminError, TransportConfig, legacy::LegacyReader};
use tokio::time::{Instant, sleep};

use super::{MAX_JSON_BYTES, clock};
use crate::configuration;

const PLAN_INTERVAL: Duration = Duration::from_secs(30);
const JOB_INTERVAL: Duration = Duration::from_secs(1);
const FIT_ZIP_ENTRIES: NonZeroUsize = match NonZeroUsize::new(4096) {
    Some(limit) => limit,
    None => NonZeroUsize::MIN,
};

#[derive(Debug)]
pub(super) enum GarminWorkerError {
    Configuration,
    Source(GarminConfigurationError),
}

impl fmt::Display for GarminWorkerError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration => output.write_str("Garmin worker configuration is invalid"),
            Self::Source(error) => error.fmt(output),
        }
    }
}
impl Error for GarminWorkerError {}

pub(super) struct GarminWorkerSettings {
    source: GarminSourceSettings,
    history_days: u16,
    backups_enabled: bool,
}

impl GarminWorkerSettings {
    pub(super) fn read() -> Result<Option<Self>, GarminWorkerError> {
        let [integrations, tokens, history, backup] = configuration::read([
            "GA_INTEGRATIONS",
            "GA_TOKEN_DIR",
            "GA_BACKFILL_DAYS",
            "GA_BACKUP_KEY",
        ])
        .map_err(|_| GarminWorkerError::Configuration)?;
        let tokens = std::path::absolute(tokens.as_deref().unwrap_or("tokens/garmin"))
            .map_err(|_| GarminWorkerError::Configuration)?;
        let Some(source) = GarminSourceSettings::resolve(integrations.as_deref(), &tokens)
            .map_err(GarminWorkerError::Source)?
        else {
            return Ok(None);
        };
        let history_days = history
            .as_deref()
            .unwrap_or("365")
            .trim()
            .parse::<u16>()
            .ok()
            .filter(|days| *days <= 3660)
            .ok_or(GarminWorkerError::Configuration)?;
        Ok(Some(Self {
            source,
            history_days,
            backups_enabled: backup.is_some_and(|key| !key.is_empty()),
        }))
    }

    pub(super) async fn run(&self, database: &Database, raw_archive: &Path, timezone: &str) -> ! {
        let mut session: Option<LegacyReader> = None;
        let mut archive = None;
        let mut next_plan = Instant::now();
        loop {
            let Ok(at) = clock() else {
                sleep(PLAN_INTERVAL).await;
                continue;
            };
            let admission = database
                .garmin_source_admission(self.source.instance_id(), at)
                .await;
            let fingerprint = match admission {
                Ok(GarminSourceAdmission::Ready { fingerprint }) => fingerprint,
                Ok(GarminSourceAdmission::Unselected) => {
                    if let Err(error) = database
                        .retire_deselected_garmin_work(self.source.instance_id(), at)
                        .await
                    {
                        eprintln!("{error}");
                    }
                    session = None;
                    sleep(PLAN_INTERVAL).await;
                    continue;
                }
                Ok(_) => {
                    session = None;
                    sleep(PLAN_INTERVAL).await;
                    continue;
                }
                Err(error) => {
                    eprintln!("{error}");
                    sleep(PLAN_INTERVAL).await;
                    continue;
                }
            };
            if session
                .as_ref()
                .is_none_or(|reader| reader.fingerprint() != fingerprint)
            {
                let tokens = match self.source.existing_tokens() {
                    Ok(Some(tokens)) => tokens,
                    Ok(None) => {
                        sleep(PLAN_INTERVAL).await;
                        continue;
                    }
                    Err(error) => {
                        eprintln!("{error}");
                        sleep(PLAN_INTERVAL).await;
                        continue;
                    }
                };
                session =
                    match LegacyReader::restore(tokens, &fingerprint, TransportConfig::default())
                        .await
                    {
                        Ok(reader) => {
                            next_plan = Instant::now();
                            Some(reader)
                        }
                        Err(error) => {
                            eprintln!("{error}");
                            if let Some(outcome) = restore_failure(error)
                                && let Err(error) = database
                                    .record_garmin_transport(&fingerprint, at, outcome)
                                    .await
                            {
                                eprintln!("{error}");
                            }
                            sleep(PLAN_INTERVAL).await;
                            continue;
                        }
                    };
            }
            let Some(reader) = session.as_ref() else {
                continue;
            };
            let archive = match &archive {
                Some(archive) => Arc::clone(archive),
                None => {
                    let opened = match Archive::open(raw_archive) {
                        Ok(opened) => Arc::new(opened),
                        Err(error) => {
                            eprintln!("{error}");
                            sleep(PLAN_INTERVAL).await;
                            continue;
                        }
                    };
                    archive = Some(Arc::clone(&opened));
                    opened
                }
            };
            let scheduled = ScheduledGarminWorker {
                runner: EndpointWorker {
                    database,
                    source: reader,
                    archive,
                    timezone,
                    limits: PutLimits {
                        max_bytes: MAX_JSON_BYTES,
                        disk_reserve_bytes: 0,
                    },
                },
                instance: self.source.instance_id(),
                history_days: self.history_days,
                backups_enabled: self.backups_enabled,
                fit_limits: ExportLimits {
                    max_bytes: MAX_FIT_BYTES,
                    max_zip_entries: FIT_ZIP_ENTRIES,
                    max_duration: Duration::from_secs(120),
                },
            };
            let Ok(now) = clock() else {
                sleep(PLAN_INTERVAL).await;
                continue;
            };
            if Instant::now() >= next_plan {
                next_plan = Instant::now() + PLAN_INTERVAL;
                match scheduled.plan(now).await {
                    Ok(GarminSyncSchedule::Paused | GarminSyncSchedule::Disabled { .. }) => {
                        session = None;
                        sleep(PLAN_INTERVAL).await;
                        continue;
                    }
                    Err(error) => {
                        eprintln!("{error}");
                        sleep(PLAN_INTERVAL).await;
                        continue;
                    }
                    Ok(_) => {}
                }
            }
            let Ok(now) = clock() else {
                sleep(PLAN_INTERVAL).await;
                continue;
            };
            match scheduled.process_one(now).await {
                Ok(GarminQueueStep::Idle) => sleep(JOB_INTERVAL).await,
                Ok(GarminQueueStep::Inactive) => {
                    session = None;
                    sleep(PLAN_INTERVAL).await;
                }
                Ok(_) => {}
                Err(error) => {
                    eprintln!("{error}");
                    sleep(JOB_INTERVAL).await;
                }
            }
        }
    }
}

fn restore_failure(error: GarminError) -> Option<GarminTransportOutcome> {
    match error {
        GarminError::AuthenticationRequired
        | GarminError::InvalidTokenResponse
        | GarminError::Unauthorized => Some(GarminTransportOutcome::AuthenticationRequired),
        GarminError::OwnerMismatch => Some(GarminTransportOutcome::AccountMismatch),
        GarminError::RateLimited { retry_after } => Some(GarminTransportOutcome::RateLimited {
            retry_after: Some(retry_after),
        }),
        GarminError::Network
        | GarminError::Timeout
        | GarminError::Upstream(_)
        | GarminError::InterruptedTransfer => {
            Some(GarminTransportOutcome::ConnectionFailure { retry_after: None })
        }
        _ => None,
    }
}
