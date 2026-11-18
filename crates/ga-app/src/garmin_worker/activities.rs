use std::{num::NonZeroU16, sync::Arc};

use ga_db::{
    activity_scans::{
        ActivityPageCapture, ActivityPagePreflight, ActivityPageResult, ActivityScanError,
        CaptureActivityPage, PublishActivityPage,
    },
    garmin_integration::GarminTransportOutcome,
    jobs::{GarminJobLease, RecordedJobStatus},
};
use ga_domain::{activity_scan::ACTIVITY_PAGE_SIZE, event_input::known_timezone};
use ga_garmin::legacy::LegacyRead;

use super::{EndpointSource, EndpointWorker, EndpointWorkerError, clock};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActivityPageWork {
    Publication(ActivityPageResult),
    Deferred(RecordedJobStatus),
}

impl<S: EndpointSource> EndpointWorker<'_, S> {
    pub async fn run_activity_page(
        &self,
        lease: &GarminJobLease,
    ) -> Result<ActivityPageWork, EndpointWorkerError> {
        if !known_timezone(self.timezone) {
            return Err(ActivityScanError::InvalidHorizon.into());
        }
        let account = self.source.fingerprint();
        let requested_at = clock()?;
        let offset = match self
            .database
            .preflight_activity_page(account, lease, requested_at)
            .await?
        {
            ActivityPagePreflight::Fetch { offset } => offset,
            ActivityPagePreflight::Superseded => {
                return Ok(ActivityPageWork::Publication(
                    ActivityPageResult::Superseded,
                ));
            }
            ActivityPagePreflight::CollectionDisabled => {
                return Ok(ActivityPageWork::Publication(
                    ActivityPageResult::CollectionDisabled,
                ));
            }
        };
        if let Some(status) = self.defer_if_paused(lease).await? {
            return Ok(ActivityPageWork::Deferred(status));
        }
        let limit = u16::try_from(ACTIVITY_PAGE_SIZE)
            .ok()
            .and_then(NonZeroU16::new)
            .ok_or(ActivityScanError::InvalidState)?;
        let resource = LegacyRead::Activities {
            start: offset,
            limit,
        };
        let response = self
            .with_renewal(lease, self.source.read(resource, self.limits.max_bytes))
            .await?;
        let body = match response {
            Ok(body) => body.unwrap_or_else(|| b"null".to_vec()),
            Err(error) => {
                let (reason, retry_at) = self.record_fetch_failure(&error).await?;
                self.database
                    .finish_activity_page_fetch_error(
                        account,
                        lease,
                        requested_at,
                        reason,
                        retry_at,
                    )
                    .await?;
                return Err(EndpointWorkerError::Fetch(error));
            }
        };
        let captured = self
            .with_renewal(
                lease,
                self.database.capture_activity_page(
                    CaptureActivityPage {
                        authenticated_fingerprint: account,
                        lease,
                        requested_at,
                        body,
                    },
                    Arc::clone(&self.archive),
                    self.limits,
                ),
            )
            .await??;
        let source = match captured {
            ActivityPageCapture::Captured(source) => source,
            ActivityPageCapture::Superseded => {
                return Ok(ActivityPageWork::Publication(
                    ActivityPageResult::Superseded,
                ));
            }
            ActivityPageCapture::CollectionDisabled => {
                return Ok(ActivityPageWork::Publication(
                    ActivityPageResult::CollectionDisabled,
                ));
            }
        };
        let published = self
            .database
            .publish_activity_page(PublishActivityPage {
                authenticated_fingerprint: account,
                lease,
                source_id: source.id,
                requested_at,
                timezone: self.timezone,
            })
            .await?;
        if matches!(published, ActivityPageResult::Published(_)) {
            self.database
                .record_garmin_transport(account, requested_at, GarminTransportOutcome::Reachable)
                .await?;
        }
        Ok(ActivityPageWork::Publication(published))
    }
}
