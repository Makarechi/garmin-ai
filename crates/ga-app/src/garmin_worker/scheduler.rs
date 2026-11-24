use ga_db::{
    garmin_integration::{GarminIntegrationError, GarminSourceAdmission},
    garmin_owner::OwnerVerificationError,
    garmin_periodic::{GarminSyncSchedule, ScheduleGarminSync},
    jobs::GarminJobKind,
};
use ga_domain::{event_input::source_calendar_date, timestamp::StoredInstant};
use ga_fit::ExportLimits;

use super::{
    ActivityExportSource, ActivityFitWork, ActivityPageWork, EndpointWork, EndpointWorker,
    EndpointWorkerError,
};

pub struct ScheduledGarminWorker<'a, S> {
    pub runner: EndpointWorker<'a, S>,
    pub instance: &'a str,
    pub history_days: u16,
    pub backups_enabled: bool,
    pub fit_limits: ExportLimits,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GarminQueueStep {
    Idle,
    Inactive,
    Endpoint(EndpointWork),
    Activities(ActivityPageWork),
    Fit(ActivityFitWork),
}

impl<S: ActivityExportSource> ScheduledGarminWorker<'_, S> {
    pub async fn plan(&self, at: StoredInstant) -> Result<GarminSyncSchedule, EndpointWorkerError> {
        self.runner
            .database
            .schedule_garmin_sync(ScheduleGarminSync {
                authenticated_fingerprint: self.runner.source.fingerprint(),
                source_instance: self.instance,
                configured_timezone: self.runner.timezone,
                history_days: self.history_days,
                now: at,
            })
            .await
            .map_err(EndpointWorkerError::Schedule)
    }

    /// Drive one due job after checking that the restored session still belongs
    /// to the current selected source. An empty queue leaves the caller idle.
    pub async fn process_one(
        &self,
        at: StoredInstant,
    ) -> Result<GarminQueueStep, EndpointWorkerError> {
        let admission = self
            .runner
            .database
            .garmin_source_admission(self.instance, at)
            .await;
        let admission = match admission {
            Ok(state) => state,
            Err(error) => return Err(error.into()),
        };
        match admission {
            GarminSourceAdmission::Ready { fingerprint }
                if fingerprint == self.runner.source.fingerprint() => {}
            GarminSourceAdmission::Ready { .. } => {
                return Err(GarminIntegrationError::Owner(OwnerVerificationError::Mismatch).into());
            }
            _ => return Ok(GarminQueueStep::Inactive),
        }
        let timezone = self
            .runner
            .database
            .garmin_job_timezone(self.runner.timezone)
            .await
            .map_err(EndpointWorkerError::Schedule)?;
        let owner_date =
            source_calendar_date(at, &timezone).ok_or(EndpointWorkerError::InvalidClock)?;
        let Some(lease) = self
            .runner
            .database
            .claim_selected_garmin_job(at, owner_date, 120, self.backups_enabled, self.instance)
            .await?
        else {
            return Ok(GarminQueueStep::Idle);
        };
        let runner = EndpointWorker {
            timezone: &timezone,
            archive: self.runner.archive.clone(),
            ..self.runner
        };
        match lease.kind() {
            GarminJobKind::Endpoint => runner.run(&lease).await.map(GarminQueueStep::Endpoint),
            GarminJobKind::Activities => runner
                .run_activity_page(&lease)
                .await
                .map(GarminQueueStep::Activities),
            GarminJobKind::Fit => runner
                .run_activity_export(&lease, self.fit_limits)
                .await
                .map(GarminQueueStep::Fit),
        }
    }
}
