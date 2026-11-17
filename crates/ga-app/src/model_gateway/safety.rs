use std::{error::Error, fmt, future::Future};

use ga_db::share_policy::ShareRequirement;
use ga_domain::{
    language_tags::normalize_language_tag, model_consent::ModelCategories,
    model_request::ApprovedModelSchema, text_offsets::from_code_points,
};
use serde_json::Value;

use super::{ActiveModelJob, CurrentModelPolicy, GatewayError, ModelGateway, ModelInput};

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
