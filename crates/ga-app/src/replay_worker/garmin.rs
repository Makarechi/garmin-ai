use std::{error::Error, fmt, path::Path, sync::Arc, time::Duration};

use ga_app::garmin_worker::{
    EndpointWorker, GarminConfigurationError, GarminQueueStep, GarminSourceSettings,
    ScheduledGarminWorker,
};
use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database, WorkTopic,
    garmin_integration::{GarminSourceAdmission, GarminTransportOutcome},
    garmin_periodic::GarminSyncSchedule,
};
use ga_garmin::{GarminError, TransportConfig, legacy::LegacyReader};
use tokio::time::{Instant, sleep};

use super::{FIT_EXPORT_LIMITS, MAX_JSON_BYTES, clock};
use crate::configuration;

const PLAN_INTERVAL: Duration = Duration::from_secs(30);
const RETRY_INTERVAL: Duration = Duration::from_secs(1);

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
}

impl GarminWorkerSettings {
    pub(super) fn read() -> Result<Option<Self>, GarminWorkerError> {
        let [integrations, tokens, history] =
            configuration::read(["GA_INTEGRATIONS", "GA_TOKEN_DIR", "GA_BACKFILL_DAYS"])
                .map_err(|_| GarminWorkerError::Configuration)?;
        let tokens = std::path::absolute(tokens.as_deref().unwrap_or("tokens/garmin"))
            .map_err(|_| GarminWorkerError::Configuration)?;
        let Some(source) = GarminSourceSettings::for_worker(integrations.as_deref(), &tokens)
            .map_err(GarminWorkerError::Source)?
        else {
            return Ok(None);
        };
        let history_days = configuration::garmin_history_days(history.as_deref().unwrap_or("365"))
            .ok_or(GarminWorkerError::Configuration)?;
        Ok(Some(Self {
            source,
            history_days,
        }))
    }

    pub(super) async fn run(
        &self,
        database: &Database,
        raw_archive: &Path,
        timezone: &str,
        backups_running: bool,
    ) -> ! {
        let mut session: Option<LegacyReader> = None;
        let mut archive = None;
        let mut next_plan = Instant::now();
        let mut work = database.work_subscription(WorkTopic::Garmin);
        let mut schedule = database.work_subscription(WorkTopic::GarminSchedule);
        loop {
            if schedule.take_pending() {
                next_plan = Instant::now();
            }
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
                    work.wait(PLAN_INTERVAL).await;
                    continue;
                }
                Ok(_) => {
                    session = None;
                    work.wait(PLAN_INTERVAL).await;
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
            let archive = if let Some(archive) = &archive {
                Arc::clone(archive)
            } else {
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
                backups_enabled: backups_running,
                fit_limits: FIT_EXPORT_LIMITS,
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
                        work.wait(PLAN_INTERVAL).await;
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
            match Box::pin(scheduled.process_one(now)).await {
                Ok(GarminQueueStep::Idle) => {
                    let wait = next_plan.saturating_duration_since(Instant::now());
                    tokio::select! {
                        biased;
                        signaled = schedule.wait(wait) => {
                            // A newly received conversation can request fresh data;
                            // ordinary queue changes only need another claim.
                            if signaled {
                                next_plan = Instant::now();
                            }
                        }
                        _ = work.wait(wait) => {}
                    }
                }
                Ok(GarminQueueStep::Inactive) => {
                    session = None;
                    work.wait(PLAN_INTERVAL).await;
                }
                Ok(_) => {}
                Err(error) => {
                    eprintln!("{error}");
                    sleep(RETRY_INTERVAL).await;
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
