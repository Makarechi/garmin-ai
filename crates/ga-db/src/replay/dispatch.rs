use std::{
    error::Error,
    fmt,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

use ga_archive::Archive;
use ga_domain::timestamp::StoredInstant;
use ga_fit::ExportLimits;
use tokio::time::{Instant, MissedTickBehavior, interval_at};

use super::{
    CalendarReplayError, CalendarReplayOutcome, GARMIN_PARSER_VERSION, HealthReplayJobResult,
    ReadReplayInput, RunHealthReplay, calendar::ReplayScope,
};
use crate::{
    Database,
    fit_ingest::{ENDPOINT as FIT_ENDPOINT, FitReplayError, FitReplayOutcome},
    jobs::{JobStoreError, RecordedJobStatus, ReplayJobLease},
};

pub struct RunReplayJob<'a> {
    pub lease: &'a ReplayJobLease,
    pub configured_timezone: &'a str,
    pub max_json_bytes: u64,
    pub fit_limits: ExportLimits,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplayJobOutcome {
    Json(CalendarReplayOutcome),
    Fit(FitReplayOutcome),
}

pub struct ReplayJobResult {
    pub replay: ReplayJobOutcome,
    pub job: RecordedJobStatus,
}

#[derive(Debug)]
pub enum ReplayWorkerError {
    Json(CalendarReplayError),
    Fit(FitReplayError),
    Lease(JobStoreError),
    InvalidWork,
    InvalidClock,
}

impl fmt::Display for ReplayWorkerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Json(error) => error.fmt(f),
            Self::Fit(error) => error.fmt(f),
            Self::Lease(error) => error.fmt(f),
            Self::InvalidWork => f.write_str("Recorded replay job is invalid"),
            Self::InvalidClock => f.write_str("Replay job clock cannot be represented"),
        }
    }
}
impl Error for ReplayWorkerError {}

impl From<JobStoreError> for ReplayWorkerError {
    fn from(value: JobStoreError) -> Self {
        Self::Lease(value)
    }
}

impl Database {
    /// Dispatch a claimed archive job and renew its lease during verification and staging.
    pub async fn run_replay_job(
        &self,
        options: RunReplayJob<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<ReplayJobResult, ReplayWorkerError> {
        let lease = options.lease;
        let cancel_on_drop = CancelOnDrop(Arc::new(AtomicBool::new(false)));
        let signal = cancel_on_drop.0.clone();
        let should_cancel = move || signal.load(Ordering::Relaxed) || cancelled();
        let run = self.dispatch_replay(options, archive, should_cancel);
        let period = lease.lease_duration() / 3;
        let mut ticks = interval_at(Instant::now() + period, period);
        ticks.set_missed_tick_behavior(MissedTickBehavior::Delay);
        let renew = async {
            loop {
                ticks.tick().await;
                if !self.renew_replay_job(lease, clock()?).await? {
                    return Err(ReplayWorkerError::Lease(JobStoreError::LeaseLost));
                }
            }
        };
        tokio::select! {
            biased;
            outcome = run => outcome,
            lost = renew => lost,
        }
    }

    async fn dispatch_replay(
        &self,
        options: RunReplayJob<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<ReplayJobResult, ReplayWorkerError> {
        let lease = options.lease;
        if lease.endpoint() == FIT_ENDPOINT {
            let (source_id, fingerprint) = lease.source().ok_or(ReplayWorkerError::InvalidWork)?;
            let (outcome, job) = self
                .replay_fit_in_job(
                    ReadReplayInput {
                        authenticated_fingerprint: &fingerprint,
                        source_id,
                        target_parser_version: GARMIN_PARSER_VERSION,
                        configured_timezone: options.configured_timezone,
                        max_json_bytes: 0,
                        max_fit_bytes: options.fit_limits.max_bytes,
                    },
                    archive,
                    options.fit_limits,
                    cancelled,
                    Some(lease),
                )
                .await
                .map_err(ReplayWorkerError::Fit)?;
            return Ok(ReplayJobResult {
                replay: ReplayJobOutcome::Fit(outcome),
                job: job.ok_or(ReplayWorkerError::InvalidWork)?,
            });
        }
        let scope = ReplayScope::for_endpoint(lease.endpoint()).map_err(ReplayWorkerError::Json)?;
        let HealthReplayJobResult { replay, job } = self
            .run_json_replay_job(
                RunHealthReplay {
                    lease,
                    configured_timezone: options.configured_timezone,
                    max_json_bytes: options.max_json_bytes,
                },
                Some(scope),
                archive,
                cancelled,
            )
            .await
            .map_err(ReplayWorkerError::Json)?;
        Ok(ReplayJobResult {
            replay: ReplayJobOutcome::Json(replay),
            job,
        })
    }
}

fn clock() -> Result<StoredInstant, ReplayWorkerError> {
    let micros = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|elapsed| i64::try_from(elapsed.as_micros()).ok())
        .ok_or(ReplayWorkerError::InvalidClock)?;
    StoredInstant::from_unix_microseconds(micros).map_err(|_| ReplayWorkerError::InvalidClock)
}

struct CancelOnDrop(Arc<AtomicBool>);

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Relaxed);
    }
}
