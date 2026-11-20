mod activities;
mod configuration;
mod fit;
mod scheduler;

pub use activities::ActivityPageWork;
pub use configuration::{
    GarminConfigurationError, GarminReauthNoticeSettings, GarminSourceSettings, garmin_auth_source,
    garmin_auth_url,
};
pub use fit::{ActivityExportSource, ActivityFitWork};
pub use scheduler::{GarminQueueStep, ScheduledGarminWorker};

use std::{
    error::Error,
    fmt,
    future::Future,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use ga_archive::{Archive, PutLimits};
use ga_db::{
    Database,
    activity_scans::ActivityScanError,
    fit_ingest::FitIngestError,
    garmin_endpoint::{
        CaptureEndpoint, EndpointCapture, EndpointError, EndpointPreflight, EndpointPublication,
        PublishEndpoint,
    },
    garmin_integration::{GarminIntegrationError, GarminRequestPause, GarminTransportOutcome},
    garmin_periodic::GarminSyncScheduleError,
    jobs::{FailureReason, GarminJobLease, JobOutcome, JobStoreError, RecordedJobStatus},
};
use ga_domain::{event_input::known_timezone, timestamp::StoredInstant};
use ga_garmin::{
    GarminError,
    legacy::{LegacyRead, LegacyReader},
};
use tokio::time::{Instant, MissedTickBehavior, interval_at};

const STORAGE_BUSY_RETRY_MICROSECONDS: i64 = 2_000_000;

pub trait EndpointSource {
    fn fingerprint(&self) -> &str;

    fn read(
        &self,
        resource: LegacyRead,
        maximum_bytes: u64,
    ) -> impl Future<Output = Result<Option<Vec<u8>>, GarminError>> + Send;
}

impl EndpointSource for LegacyReader {
    fn fingerprint(&self) -> &str {
        self.fingerprint()
    }

    async fn read(
        &self,
        resource: LegacyRead,
        maximum_bytes: u64,
    ) -> Result<Option<Vec<u8>>, GarminError> {
        self.get_json(resource, maximum_bytes).await
    }
}

pub struct EndpointWorker<'a, S> {
    pub database: &'a Database,
    pub source: &'a S,
    pub archive: Arc<Archive>,
    pub limits: PutLimits,
    pub timezone: &'a str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EndpointWork {
    Publication(EndpointPublication),
    Deferred(RecordedJobStatus),
}

#[derive(Debug)]
pub enum EndpointWorkerError {
    Storage(EndpointError),
    ActivityScan(ActivityScanError),
    Fit(FitIngestError),
    Job(JobStoreError),
    Integration(GarminIntegrationError),
    Schedule(GarminSyncScheduleError),
    Fetch(GarminError),
    InvalidClock,
}

impl fmt::Display for EndpointWorkerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Storage(error) => error.fmt(f),
            Self::ActivityScan(error) => error.fmt(f),
            Self::Fit(error) => error.fmt(f),
            Self::Job(error) => error.fmt(f),
            Self::Integration(error) => error.fmt(f),
            Self::Schedule(error) => error.fmt(f),
            Self::Fetch(error) => error.fmt(f),
            Self::InvalidClock => f.write_str("Garmin request clock cannot be represented"),
        }
    }
}
impl Error for EndpointWorkerError {}

impl From<EndpointError> for EndpointWorkerError {
    fn from(value: EndpointError) -> Self {
        Self::Storage(value)
    }
}
impl From<ActivityScanError> for EndpointWorkerError {
    fn from(value: ActivityScanError) -> Self {
        Self::ActivityScan(value)
    }
}
impl From<FitIngestError> for EndpointWorkerError {
    fn from(value: FitIngestError) -> Self {
        Self::Fit(value)
    }
}
impl From<JobStoreError> for EndpointWorkerError {
    fn from(value: JobStoreError) -> Self {
        Self::Job(value)
    }
}
impl From<GarminIntegrationError> for EndpointWorkerError {
    fn from(value: GarminIntegrationError) -> Self {
        Self::Integration(value)
    }
}

impl<S: EndpointSource> EndpointWorker<'_, S> {
    pub async fn run(&self, lease: &GarminJobLease) -> Result<EndpointWork, EndpointWorkerError> {
        match self.run_endpoint(lease).await {
            Err(EndpointWorkerError::Storage(error)) if error.storage_busy() => {
                let retry = clock()?.unix_microseconds() + STORAGE_BUSY_RETRY_MICROSECONDS;
                let retry = StoredInstant::from_unix_microseconds(retry)
                    .map_err(|_| EndpointWorkerError::InvalidClock)?;
                self.defer(lease, FailureReason::GarminStorageBusy, Some(retry))
                    .await
                    .map(EndpointWork::Deferred)
            }
            result => result,
        }
    }

    async fn run_endpoint(
        &self,
        lease: &GarminJobLease,
    ) -> Result<EndpointWork, EndpointWorkerError> {
        if !known_timezone(self.timezone) {
            return Err(EndpointError::InvalidScope.into());
        }
        let at = clock()?;
        let account = self.source.fingerprint();
        let preflight = self
            .database
            .preflight_garmin_endpoint(account, lease, at)
            .await?;
        let (endpoint, key) = match preflight {
            EndpointPreflight::Fetch {
                endpoint,
                source_key,
            } => (endpoint, source_key),
            EndpointPreflight::CollectionDisabled => {
                return Ok(EndpointWork::Publication(
                    EndpointPublication::CollectionDisabled,
                ));
            }
            EndpointPreflight::Superseded => {
                return Ok(EndpointWork::Publication(EndpointPublication::Superseded));
            }
        };
        if let Some(status) = self.defer_if_paused(lease).await? {
            return Ok(EndpointWork::Deferred(status));
        }
        let resource = match LegacyRead::queued_endpoint(&endpoint, &key) {
            Ok(resource) => resource,
            Err(_) => {
                return self
                    .defer(lease, FailureReason::InvalidGarminWork, None)
                    .await
                    .map(EndpointWork::Deferred);
            }
        };

        let response = self
            .with_renewal(lease, self.source.read(resource, self.limits.max_bytes))
            .await?;
        let body = match response {
            Ok(bytes) => bytes.unwrap_or_else(|| b"null".to_vec()),
            Err(error) => return self.fetch_failed(lease, at, error).await,
        };
        let captured = self
            .with_renewal(
                lease,
                self.database.capture_garmin_endpoint(
                    CaptureEndpoint {
                        authenticated_fingerprint: account,
                        lease,
                        requested_at: at,
                        body,
                    },
                    Arc::clone(&self.archive),
                    self.limits,
                ),
            )
            .await??;
        let captured = match captured {
            EndpointCapture::Captured(source) => source,
            EndpointCapture::CollectionDisabled => {
                return Ok(EndpointWork::Publication(
                    EndpointPublication::CollectionDisabled,
                ));
            }
            EndpointCapture::Superseded => {
                return Ok(EndpointWork::Publication(EndpointPublication::Superseded));
            }
        };
        let result = self
            .database
            .publish_garmin_endpoint(PublishEndpoint {
                authenticated_fingerprint: account,
                lease,
                source_id: captured.id,
                requested_at: at,
                timezone: self.timezone,
                replacement: None,
            })
            .await?;
        if matches!(result, EndpointPublication::Published(_)) {
            self.note_reachable(account, at).await?;
        }
        Ok(EndpointWork::Publication(result))
    }

    async fn note_reachable(
        &self,
        account: &str,
        at: StoredInstant,
    ) -> Result<(), EndpointWorkerError> {
        match self
            .database
            .record_garmin_transport(account, at, GarminTransportOutcome::Reachable)
            .await
        {
            // Publication already committed. A later successful request refreshes
            // the connection state if the queue lock stays busy beyond its budget.
            Ok(_) | Err(GarminIntegrationError::Busy) => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    async fn defer_if_paused(
        &self,
        lease: &GarminJobLease,
    ) -> Result<Option<RecordedJobStatus>, EndpointWorkerError> {
        let Some(pause) = self.database.garmin_request_pause(clock()?).await? else {
            return Ok(None);
        };
        let retry_at = match pause {
            GarminRequestPause::Reauthentication => None,
            GarminRequestPause::Cooldown(until) => Some(until),
        };
        self.defer(lease, FailureReason::ProviderCooldown, retry_at)
            .await
            .map(Some)
    }

    async fn defer(
        &self,
        lease: &GarminJobLease,
        reason: FailureReason,
        retry_at: Option<StoredInstant>,
    ) -> Result<RecordedJobStatus, EndpointWorkerError> {
        let status = self
            .database
            .finish_garmin_job(lease, clock()?, JobOutcome::Failed { reason, retry_at })
            .await?;
        Ok(status)
    }

    async fn fetch_failed(
        &self,
        lease: &GarminJobLease,
        attempted: StoredInstant,
        error: GarminError,
    ) -> Result<EndpointWork, EndpointWorkerError> {
        let (reason, retry_at) = self.record_fetch_failure(&error).await?;
        self.database
            .finish_garmin_endpoint_fetch_error(
                self.source.fingerprint(),
                lease,
                attempted,
                reason,
                retry_at,
            )
            .await?;
        Err(EndpointWorkerError::Fetch(error))
    }

    async fn record_fetch_failure(
        &self,
        error: &GarminError,
    ) -> Result<(FailureReason, Option<StoredInstant>), EndpointWorkerError> {
        let account = self.source.fingerprint();
        let (outcome, reason) = match error {
            GarminError::AuthenticationRequired
            | GarminError::Unauthorized
            | GarminError::InvalidTokenResponse => (
                Some(GarminTransportOutcome::AuthenticationRequired),
                FailureReason::GarminAuthenticationRequired,
            ),
            GarminError::OwnerMismatch => (
                Some(GarminTransportOutcome::AccountMismatch),
                FailureReason::GarminAuthenticationRequired,
            ),
            GarminError::RateLimited { retry_after } => (
                Some(GarminTransportOutcome::RateLimited {
                    retry_after: Some(*retry_after),
                }),
                FailureReason::ProviderCooldown,
            ),
            GarminError::Network
            | GarminError::Timeout
            | GarminError::Upstream(_)
            | GarminError::InterruptedTransfer => (
                Some(GarminTransportOutcome::ConnectionFailure { retry_after: None }),
                FailureReason::RuntimeError,
            ),
            _ => (None, FailureReason::RuntimeError),
        };
        let retry_at = match outcome {
            Some(outcome) => {
                self.database
                    .record_garmin_transport(account, clock()?, outcome)
                    .await?
                    .blocked_until
            }
            None => None,
        };
        Ok((reason, retry_at))
    }

    async fn with_renewal<T>(
        &self,
        lease: &GarminJobLease,
        work: impl Future<Output = T>,
    ) -> Result<T, EndpointWorkerError> {
        self.with_renewal_until(lease, work, std::future::pending())
            .await
    }

    async fn with_renewal_until<T>(
        &self,
        lease: &GarminJobLease,
        work: impl Future<Output = T>,
        stop_renewal: impl Future<Output = ()>,
    ) -> Result<T, EndpointWorkerError> {
        let period = lease.lease_duration() / 3;
        let mut ticks = interval_at(Instant::now() + period, period);
        ticks.set_missed_tick_behavior(MissedTickBehavior::Delay);
        let renew = async {
            loop {
                ticks.tick().await;
                if !self.database.renew_garmin_job(lease, clock()?).await? {
                    return Err(JobStoreError::LeaseLost.into());
                }
            }
        };
        tokio::select! {
            biased;
            result = work => Ok(result),
            lost = async {
                tokio::select! {
                    biased;
                    () = stop_renewal => std::future::pending().await,
                    lost = renew => lost,
                }
            } => lost,
        }
    }
}

fn clock() -> Result<StoredInstant, EndpointWorkerError> {
    let micros = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|duration| i64::try_from(duration.as_micros()).ok())
        .ok_or(EndpointWorkerError::InvalidClock)?;
    StoredInstant::from_unix_microseconds(micros).map_err(|_| EndpointWorkerError::InvalidClock)
}
