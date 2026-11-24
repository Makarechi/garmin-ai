#[cfg(test)]
mod tests;

use ga_db::{
    diary::actions::{TrackerTextContext, TrackerTextError, TrackerTextPreparation},
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::{
    model_consent::ModelCategories,
    model_request::ApprovedModelSchema,
    tracker_nl::{TrackerExtraction, TrackerTextRequest},
};
use serde_json::Value;

use super::{CurrentModelPolicy, ModelGateway, ModelInput, Sender};

const INSTRUCTION: &str = include_str!("../../../../src/garmin_ai/prompts/tracker_extract.txt");

impl ModelGateway<'_> {
    pub(crate) async fn process_telegram_tracker_text<'policy, S: Sender>(
        &self,
        lease: &ga_db::jobs::telegram_inbound::TelegramInboundLease,
        timezone: &str,
        locale: &str,
        policy: &(impl Fn() -> CurrentModelPolicy<'policy> + Sync),
        sender: &S,
    ) -> Result<(), ga_db::jobs::telegram_inbound::TelegramDiaryCommitError> {
        use super::{ActiveModelJob, runtime::ModelRequestScope};
        use ga_db::{
            jobs::telegram_inbound::{
                TelegramDiaryCommitError, TelegramTrackerTextPreparation, TrackerSafetyStatus,
            },
            share_policy::ModelChannelSharing,
        };

        let envelope = lease
            .processing_envelope()
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        let channel = format!("telegram:{}", envelope.channel().instance_id());
        let channel = ShareDestination::new(DestinationKind::Channel, &channel)
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        let (id, lease_token) = lease.job_identity();
        let job = Some(ActiveModelJob { id, lease_token });
        let safety_requirements = [self
            .database
            .telegram_tracker_model_requirement(lease)
            .await?];
        let destination = ShareDestination::new(DestinationKind::Model, self.selected.instance_id)
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        let current = policy();
        let cached = if current.configuration.enabled && current.consent.is_some() {
            self.database
                .cached_telegram_tracker_safety(lease, safety_requirements[0], destination)
                .await?
        } else {
            Some(TrackerSafetyStatus::Unavailable)
        };
        let status = if let Some(status) = cached {
            status
        } else {
            let screen = self
                .execute_with_context(
                    ModelInput::Structured {
                        contract: ApprovedModelSchema::SafetyScreen,
                        instruction: include_str!(
                            "../../../../src/garmin_ai/prompts/form_safety.txt"
                        )
                        .trim(),
                        prompt: envelope
                            .text()
                            .ok_or(TelegramDiaryCommitError::UnsupportedMessage)?,
                        categories: ModelCategories::HEALTH_AND_DIARY,
                    },
                    policy,
                    ModelRequestScope {
                        uses_voice_text: lease.has_voice_text(),
                        requirements: &safety_requirements,
                        channel: Some(ModelChannelSharing {
                            destination: channel,
                            requirements: &[],
                            conversation: None,
                        }),
                        job,
                    },
                    sender,
                )
                .await;
            let status = match screen
                .ok()
                .and_then(|screen| screen.value["urgent"].as_bool())
            {
                Some(true) => TrackerSafetyStatus::Urgent,
                Some(false) => TrackerSafetyStatus::Checked,
                None => TrackerSafetyStatus::Unavailable,
            };
            self.database
                .record_telegram_tracker_safety(lease, safety_requirements[0].version_id, status)
                .await?;
            status
        };
        if status == TrackerSafetyStatus::Urgent {
            return self
                .database
                .finish_telegram_model_notice(lease, crate::urgent_screen::urgent_notice(locale))
                .await;
        }
        let safety_unavailable = status == TrackerSafetyStatus::Unavailable;
        let session = match self
            .database
            .prepare_telegram_tracker_text(lease, timezone, locale, destination, safety_unavailable)
            .await?
        {
            TelegramTrackerTextPreparation::Complete => return Ok(()),
            TelegramTrackerTextPreparation::Model(session) => session,
        };
        let prompt = session
            .prompt()
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        let requirements = session
            .sharing_requirements()
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        let channel_requirements = [session.channel_requirement()];
        let response = self
            .execute_with_context(
                ModelInput::Structured {
                    contract: ApprovedModelSchema::TrackerExtraction,
                    instruction: INSTRUCTION.trim(),
                    prompt: &prompt,
                    categories: ModelCategories::DIARY,
                },
                policy,
                ModelRequestScope {
                    uses_voice_text: lease.has_voice_text(),
                    requirements: &requirements,
                    channel: Some(ModelChannelSharing {
                        destination: channel,
                        requirements: &channel_requirements,
                        conversation: None,
                    }),
                    job,
                },
                sender,
            )
            .await;
        let extraction = response
            .ok()
            .and_then(|response| TrackerExtraction::from_value(&response.value).ok());
        session
            .finish(&self.database, lease, extraction, safety_unavailable)
            .await
    }

    pub async fn process_tracker_text<'policy>(
        &self,
        request: &TrackerTextRequest,
        context: TrackerTextContext<'_>,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
    ) -> Result<Value, TrackerTextError> {
        self.tracker_text_with(request, context, &policy, &self.transport)
            .await
    }

    async fn tracker_text_with<'policy, S: Sender>(
        &self,
        request: &TrackerTextRequest,
        context: TrackerTextContext<'_>,
        policy: &(impl Fn() -> CurrentModelPolicy<'policy> + Sync),
        sender: &S,
    ) -> Result<Value, TrackerTextError> {
        let destination = ShareDestination::new(DestinationKind::Model, self.selected.instance_id)
            .map_err(|_| TrackerTextError::Database)?;
        let session = match self
            .database
            .prepare_tracker_text(request, context, Some(destination))
            .await?
        {
            TrackerTextPreparation::Complete(value) => return Ok(value),
            TrackerTextPreparation::Model(session) => session,
        };
        let prompt = session.prompt()?;
        let requirements = session.sharing_requirements()?;
        let response = self
            .execute_with(
                ModelInput::Structured {
                    contract: ApprovedModelSchema::TrackerExtraction,
                    instruction: INSTRUCTION.trim(),
                    prompt: &prompt,
                    categories: ModelCategories::DIARY,
                },
                policy,
                &requirements,
                None,
                sender,
            )
            .await;
        let Some(extraction) = response
            .ok()
            .and_then(|response| TrackerExtraction::from_value(&response.value).ok())
        else {
            return session.fallback().await;
        };
        session.apply(extraction).await
    }
}
