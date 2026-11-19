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
        let extraction = match response
            .ok()
            .and_then(|response| TrackerExtraction::from_value(&response.value).ok())
        {
            Some(extraction) => extraction,
            None => return session.fallback().await,
        };
        session.apply(extraction).await
    }
}
