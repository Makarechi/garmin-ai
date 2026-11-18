use std::{future::Future, num::NonZeroU64, sync::Arc};

use ga_archive::{ArchiveError, PutLimits};
use ga_db::{
    fit_ingest::{FitIngestError, FitIngestOutcome, LeasedFitLimits, LegacyFitCapture},
    garmin_integration::GarminTransportOutcome,
    jobs::{FailureReason, GarminJobKind, GarminJobLease, JobOutcome, RecordedJobStatus},
    raw_capture::CaptureError,
};
use ga_fit::ExportLimits;
use ga_garmin::{GarminError, legacy::LegacyReader};
use serde_json::Value;
use tokio::{
    fs::File,
    io::{AsyncSeekExt, AsyncWriteExt},
};

use super::{EndpointSource, EndpointWorker, EndpointWorkerError, clock};

pub trait ActivityExportSource: EndpointSource {
    fn download(
        &self,
        activity: NonZeroU64,
        maximum_bytes: u64,
        destination: &mut File,
    ) -> impl Future<Output = Result<u64, GarminError>> + Send;
}

impl ActivityExportSource for LegacyReader {
    async fn download(
        &self,
        activity: NonZeroU64,
        maximum_bytes: u64,
        destination: &mut File,
    ) -> Result<u64, GarminError> {
        self.download_activity(activity, maximum_bytes, destination)
            .await
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActivityFitWork {
    Publication(FitIngestOutcome),
    Deferred(RecordedJobStatus),
    CollectionDisabled,
}

impl<S: ActivityExportSource> EndpointWorker<'_, S> {
    pub async fn run_activity_export(
        &self,
        lease: &GarminJobLease,
        export_limits: ExportLimits,
    ) -> Result<ActivityFitWork, EndpointWorkerError> {
        if lease.kind() != GarminJobKind::Fit {
            return Err(FitIngestError::WrongJob.into());
        }
        let payload: Value =
            serde_json::from_str(lease.payload_json()).map_err(|_| FitIngestError::WrongJob)?;
        let Some((activity_id, provider_id)) = payload["activity_id"]
            .as_str()
            .and_then(|id| id.parse::<NonZeroU64>().ok().map(|parsed| (id, parsed)))
        else {
            let status = self
                .defer(lease, FailureReason::InvalidGarminWork, None)
                .await?;
            return Ok(ActivityFitWork::Deferred(status));
        };
        let requested_at = clock()?;
        let request = LegacyFitCapture {
            activity_id,
            authenticated_fingerprint: self.source.fingerprint(),
            fetched_at: requested_at,
        };
        match self
            .database
            .authorize_legacy_fit_job(&request, lease)
            .await
        {
            Err(FitIngestError::Access(CaptureError::CollectionDisabled)) => {
                return self.finish_disabled_export(lease).await;
            }
            result => result?,
        }
        if let Some(status) = self.defer_if_paused(lease).await? {
            return Ok(ActivityFitWork::Deferred(status));
        }
        let put_limits = PutLimits {
            max_bytes: export_limits.max_bytes,
            disk_reserve_bytes: self.limits.disk_reserve_bytes,
        };
        let (file, download_limit) = self
            .archive
            .open_download_staging(put_limits)
            .map_err(FitIngestError::Archive)?;
        let mut file = File::from_std(file);
        let received = self
            .with_renewal(
                lease,
                self.source.download(provider_id, download_limit, &mut file),
            )
            .await?;
        let received = match received {
            Ok(received) => received,
            Err(error) => return self.export_fetch_failed(lease, request, error).await,
        };
        file.flush().await.map_err(archive_io)?;
        file.rewind().await.map_err(archive_io)?;
        let file = file.into_std().await;
        let recorded = file.metadata().map_err(archive_io)?.len();
        if received == 0 || recorded != received || received > download_limit {
            return self
                .export_fetch_failed(lease, request, GarminError::InterruptedTransfer)
                .await;
        }
        let (publishing, stop_renewal) = tokio::sync::oneshot::channel();
        let published = self
            .with_renewal_until(
                lease,
                self.database.ingest_leased_legacy_fit_before_publish(
                    request,
                    lease,
                    file,
                    Arc::clone(&self.archive),
                    LeasedFitLimits {
                        archive: put_limits,
                        projection: export_limits,
                    },
                    || {
                        let _ = publishing.send(());
                    },
                ),
                async {
                    let _ = stop_renewal.await;
                },
            )
            .await?;
        match published {
            Err(FitIngestError::Access(CaptureError::CollectionDisabled)) => {
                self.finish_disabled_export(lease).await
            }
            Err(error) => Err(error.into()),
            Ok(outcome) => {
                if matches!(
                    outcome,
                    FitIngestOutcome::Normalized { .. }
                        | FitIngestOutcome::Unchanged
                        | FitIngestOutcome::Empty
                ) {
                    self.database
                        .record_garmin_transport(
                            self.source.fingerprint(),
                            requested_at,
                            GarminTransportOutcome::Reachable,
                        )
                        .await?;
                }
                Ok(ActivityFitWork::Publication(outcome))
            }
        }
    }

    async fn finish_disabled_export(
        &self,
        lease: &GarminJobLease,
    ) -> Result<ActivityFitWork, EndpointWorkerError> {
        self.database
            .finish_garmin_job(lease, clock()?, JobOutcome::Completed)
            .await?;
        Ok(ActivityFitWork::CollectionDisabled)
    }

    async fn export_fetch_failed(
        &self,
        lease: &GarminJobLease,
        request: LegacyFitCapture<'_>,
        error: GarminError,
    ) -> Result<ActivityFitWork, EndpointWorkerError> {
        let (reason, retry_at) = self.record_fetch_failure(&error).await?;
        match self
            .database
            .finish_legacy_fit_fetch_error(request, lease, reason, retry_at)
            .await
        {
            Err(FitIngestError::Access(CaptureError::CollectionDisabled)) => {
                self.finish_disabled_export(lease).await
            }
            result => {
                result?;
                Err(EndpointWorkerError::Fetch(error))
            }
        }
    }
}

fn archive_io(error: std::io::Error) -> FitIngestError {
    FitIngestError::Archive(ArchiveError::from(error))
}
