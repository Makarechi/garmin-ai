use std::{error::Error, fmt, future::Future};

use ga_db::{
    jobs::telegram_inbound::{TelegramDiaryCommitError, TelegramInboundLease},
    share_policy::{DestinationKind, ModelChannelSharing, ShareDestination, ShareRequirement},
};
use ga_domain::{
    language_tags::normalize_language_tag, model_consent::ModelCategories,
    model_request::ApprovedModelSchema, text_offsets::from_code_points,
};
use serde_json::Value;

use super::{
    ActiveModelJob, CurrentModelPolicy, GatewayError, ModelGateway, ModelInput, Sender,
    runtime::ModelRequestScope,
};

const REPLY_LIMIT: usize = 16_000;
const TOTAL_CHARS: usize = 48_000;
const FRAGMENT_CHARS: usize = 12_000;
const OVERLAP: usize = 256;
const REPLY_PROMPT: &str = include_str!("../../../../src/garmin_ai/prompts/safety_reply.txt");
const FRAGMENT_PROMPT: &str = include_str!("../../../../src/garmin_ai/prompts/safety_fragment.txt");

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplySafetyIntent {
    Clarify,
    Safety,
}

#[derive(Debug, Eq, PartialEq)]
pub struct ReplySafety {
    pub intent: ReplySafetyIntent,
    pub language: String,
    pub oversized_notice: bool,
}

#[derive(Debug)]
pub enum SafetyScreenError {
    Model(GatewayError),
    InvalidResponse,
}

impl fmt::Display for SafetyScreenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Model(error) => error.fmt(f),
            Self::InvalidResponse => f.write_str("Safety screening returned invalid data"),
        }
    }
}
impl Error for SafetyScreenError {}

impl ModelGateway<'_> {
    /// Screen exact owner text using the original prompts and the live health and diary
    /// consent check before each fragment can leave the service.
    pub async fn screen_reply_safety<'policy>(
        &self,
        text: &str,
        locale: &str,
        policy: impl Fn() -> CurrentModelPolicy<'policy> + Sync,
        requirements: &[ShareRequirement<'_>],
        job: Option<ActiveModelJob>,
    ) -> Result<ReplySafety, SafetyScreenError> {
        screen_with(text, locale, |instruction, prompt| async {
            let output = self
                .execute(
                    ModelInput::Structured {
                        contract: ApprovedModelSchema::SafetyScreen,
                        instruction,
                        prompt,
                        categories: ModelCategories::HEALTH_AND_DIARY,
                    },
                    &policy,
                    requirements,
                    job,
                )
                .await
                .map_err(SafetyScreenError::Model)?;
            Ok(output.value)
        })
        .await
    }

    pub(crate) async fn screen_voice_reply_safety_with<'policy, S: Sender>(
        &self,
        lease: &TelegramInboundLease,
        locale: &str,
        policy: &(impl Fn() -> CurrentModelPolicy<'policy> + Sync),
        sender: &S,
    ) -> Result<ReplySafety, TelegramDiaryCommitError> {
        let envelope = lease
            .processing_envelope()
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        let text = envelope
            .text()
            .ok_or(TelegramDiaryCommitError::UnsupportedMessage)?;
        let model = ShareDestination::new(DestinationKind::Model, self.binding().instance_id)
            .map_err(|_| TelegramDiaryCommitError::VoiceConsentChanged)?;
        let instance = format!("telegram:{}", envelope.channel().instance_id());
        let destination = ShareDestination::new(DestinationKind::Channel, &instance)
            .map_err(|_| TelegramDiaryCommitError::BindingChanged)?;
        let requirements = self
            .database
            .telegram_voice_sharing(lease, model)
            .await?
            .ok_or(TelegramDiaryCommitError::VoiceConsentChanged)?;
        let (id, lease_token) = lease.job_identity();
        let safety = screen_with(text, locale, |instruction, prompt| {
            let request = self.execute_with_context(
                ModelInput::Structured {
                    contract: ApprovedModelSchema::SafetyScreen,
                    instruction,
                    prompt,
                    categories: ModelCategories::HEALTH_AND_DIARY,
                },
                policy,
                ModelRequestScope {
                    uses_voice_text: true,
                    requirements: &requirements,
                    channel: Some(ModelChannelSharing {
                        destination,
                        requirements: &[],
                        conversation: None,
                    }),
                    job: Some(ActiveModelJob { id, lease_token }),
                },
                sender,
            );
            async move {
                request
                    .await
                    .map(|output| output.value)
                    .map_err(SafetyScreenError::Model)
            }
        })
        .await
        .unwrap_or_else(|_| decision(true, true, locale.to_owned()));
        Ok(safety)
    }
}

async fn screen_with<'a, F, Response>(
    text: &'a str,
    locale: &str,
    mut call: F,
) -> Result<ReplySafety, SafetyScreenError>
where
    F: FnMut(&'static str, &'a str) -> Response,
    Response: Future<Output = Result<Value, SafetyScreenError>>,
{
    let total = text.chars().take(TOTAL_CHARS + OVERLAP + 1).count();
    let mut language = locale.to_owned();
    if total <= REPLY_LIMIT {
        let raw = call(REPLY_PROMPT.trim(), text).await?;
        let urgent = use_response(&raw, &mut language)?;
        return Ok(decision(urgent, false, language));
    }

    for start in (0..total.min(TOTAL_CHARS)).step_by(FRAGMENT_CHARS) {
        let end = total.min(start + FRAGMENT_CHARS + OVERLAP);
        let Some(offsets) = from_code_points(text, start..end) else {
            return Ok(decision(true, true, language));
        };
        let screened = call(FRAGMENT_PROMPT.trim(), &text[offsets.utf8]).await;
        match screened.and_then(|raw| use_response(&raw, &mut language)) {
            Err(_) => return Ok(decision(true, true, language)),
            Ok(true) => return Ok(decision(true, false, language)),
            Ok(false) => {}
        }
    }
    Ok(decision(total > TOTAL_CHARS, true, language))
}

fn use_response(raw: &Value, language: &mut String) -> Result<bool, SafetyScreenError> {
    let decision = raw["urgent"]
        .as_bool()
        .ok_or(SafetyScreenError::InvalidResponse)?;
    match &raw["response_language"] {
        Value::Null => {}
        Value::String(tag) => {
            *language = normalize_language_tag(tag).ok_or(SafetyScreenError::InvalidResponse)?;
        }
        _ => return Err(SafetyScreenError::InvalidResponse),
    }
    Ok(decision)
}

fn decision(urgent: bool, oversized_notice: bool, language: String) -> ReplySafety {
    ReplySafety {
        intent: if urgent {
            ReplySafetyIntent::Safety
        } else {
            ReplySafetyIntent::Clarify
        },
        language,
        oversized_notice,
    }
}

#[cfg(test)]
mod tests;
