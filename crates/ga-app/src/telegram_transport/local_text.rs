use std::{sync::OnceLock, time::Duration};

use fancy_regex::Regex;
use ga_db::{
    Database,
    jobs::telegram_inbound::{
        LocalTelegramText, SetupMetadata, TelegramDiaryCommitError, TelegramInboundLease,
        TelegramInboundState, TelegramInboundTarget, TelegramSafetyDecision,
    },
    outbox_enqueue::QueueIntentError,
};
use ga_domain::{
    channel::InboundEnvelope, event_payload::python_whitespace,
    event_schema::parse_tracker_chat_field,
};
use tokio::time::sleep;

use super::{TelegramTransport, poller::wall_clock};
use crate::{
    telegram_settings::TelegramIngressSettings,
    urgent_screen::{
        UrgentScreenUnavailable, obvious_third_party_emergency, obvious_urgent_symptoms,
        urgent_notice,
    },
};

const LOCAL_LEASE: u32 = 10;
const IDLE: Duration = Duration::from_millis(250);

impl TelegramTransport {
    /// Give local safety screening its own turn even while older reports await a provider.
    pub async fn run_local_text(
        &self,
        database: &Database,
        settings: &TelegramIngressSettings,
        locale: &str,
        provider_available: bool,
    ) -> ! {
        loop {
            if let (Ok(binding), Ok(now)) =
                (settings.confirmed_binding(database).await, wall_clock())
            {
                let target = TelegramInboundTarget {
                    owner_id: binding.owner_id,
                    instance_id: binding.instance_id,
                };
                if let Ok(Some(lease)) = database
                    .claim_telegram_local_screen(target, now, LOCAL_LEASE)
                    .await
                    && let Ok(decision) = local_screen(&lease)
                {
                    let result = database
                        .apply_local_telegram_text(
                            &lease,
                            LocalTelegramText::Safety(decision),
                            locale,
                        )
                        .await;
                    if complete_or_retry(database, &lease, result).await {
                        continue;
                    }
                }
                if !provider_available
                    && let Ok(Some(lease)) = database
                        .claim_telegram_no_model_text(target, now, LOCAL_LEASE)
                        .await
                {
                    let result = database
                        .apply_local_telegram_text(
                            &lease,
                            LocalTelegramText::ProviderUnavailable,
                            locale,
                        )
                        .await;
                    if complete_or_retry(database, &lease, result).await {
                        continue;
                    }
                }
            }
            sleep(IDLE).await;
        }
    }
}

fn local_screen(
    lease: &TelegramInboundLease,
) -> Result<TelegramSafetyDecision<'static>, UrgentScreenUnavailable> {
    let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
        .map_err(|_| UrgentScreenUnavailable)?;
    let text = envelope.text().ok_or(UrgentScreenUnavailable)?;
    if !obvious_urgent_symptoms(text)? {
        return Ok(TelegramSafetyDecision::Clear);
    }
    Ok(TelegramSafetyDecision::Urgent {
        russian: urgent_notice("ru"),
        english: urgent_notice("en"),
        setup_metadata: setup_metadata(text)?,
    })
}

struct SetupPatterns {
    label_is_report: Regex,
    tracker_title: Regex,
    title_personal: Regex,
    name_is_report: Regex,
    acute: Regex,
    breathing: Regex,
}

impl SetupPatterns {
    fn build() -> Result<Self, fancy_regex::Error> {
        Ok(Self {
            label_is_report: Regex::new(
                r"(?i)\b(?:i|my|we|our|я|мне|меня|мой|моя|моё|мои|нас|наш\w*)\b",
            )?,
            tracker_title: Regex::new(
                r"(?i)\b(?:diary|tracker|journal|log|recovery|дневник|журнал|трекер|восстановление)\b",
            )?,
            title_personal: Regex::new(r"(?i)\b(?:i|my|у меня|я|мне|мой|моя|моё)\b")?,
            name_is_report: Regex::new(
                r"(?i)\b(?:i|my|me|he|she|they|someone|я|мне|у меня|у него|у неё)\b",
            )?,
            acute: Regex::new(r"(?i)\b(?:sudden|acute|внезапн\w*|резк\w*)\b")?,
            breathing: Regex::new(
                r"(?i)\b(?:can't|cannot|can\s+not|unable\s+to|struggling\s+to)\s+breathe\b|\bне\s+могу\s+дышать\b",
            )?,
        })
    }
}

fn field_label(text: &str) -> Option<&str> {
    parse_tracker_chat_field(text.trim_matches(python_whitespace), "field").ok()?;
    text.split_once('|')
        .map(|(label, _)| label.trim_matches(python_whitespace))
}

fn setup_metadata(text: &str) -> Result<SetupMetadata, UrgentScreenUnavailable> {
    if obvious_third_party_emergency(text)? {
        return Ok(SetupMetadata::None);
    }
    static PATTERNS: OnceLock<Result<SetupPatterns, fancy_regex::Error>> = OnceLock::new();
    let patterns = PATTERNS
        .get_or_init(SetupPatterns::build)
        .as_ref()
        .map_err(|_| UrgentScreenUnavailable)?;
    if let Some(label) = field_label(text)
        && !(patterns.label_is_report.is_match(label)? && obvious_urgent_symptoms(label)?)
    {
        return Ok(SetupMetadata::Field);
    }
    if patterns.acute.is_match(text)? {
        return Ok(SetupMetadata::None);
    }
    let normalized = text.trim().to_lowercase();
    let symptom_title = matches!(
        normalized.as_str(),
        "stroke" | "heart attack" | "seizure" | "инсульт" | "сердечный приступ" | "судороги"
    ) || (patterns.tracker_title.is_match(text)?
        && !patterns.title_personal.is_match(text)?);
    Ok(
        if symptom_title
            || (!patterns.name_is_report.is_match(text)? && !patterns.breathing.is_match(text)?)
        {
            SetupMetadata::Name
        } else {
            SetupMetadata::None
        },
    )
}

pub(super) async fn complete_or_retry(
    database: &Database,
    lease: &TelegramInboundLease,
    result: Result<(), TelegramDiaryCommitError>,
) -> bool {
    match result {
        Ok(()) => true,
        Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy)) => {
            if let Ok(now) = wall_clock() {
                let _ = database.defer_telegram_owner_control(lease, now).await;
            }
            false
        }
        Err(TelegramDiaryCommitError::UnsupportedMessage) => {
            if let Ok(now) = wall_clock() {
                let _ = database
                    .finish_telegram_inbound(lease, now, TelegramInboundState::Invalid)
                    .await;
            }
            false
        }
        Err(_) => false,
    }
}

#[cfg(test)]
mod setup_metadata_tests {
    use super::*;

    #[test]
    fn preserves_personal_emergencies_while_accepting_explicit_tracker_metadata() {
        for text in [
            "I can't breathe",
            "Я не могу дышать",
            "I can't breathe | text",
        ] {
            assert!(
                matches!(setup_metadata(text), Ok(SetupMetadata::None)),
                "{text}"
            );
        }
        assert!(matches!(
            setup_metadata("Chest pain diary"),
            Ok(SetupMetadata::Name)
        ));
        assert!(matches!(
            setup_metadata("Sudden severe chest pain | text"),
            Ok(SetupMetadata::Field)
        ));
        assert!(matches!(
            setup_metadata("Sudden severe chest pain | ſcale 1-5"),
            Ok(SetupMetadata::Field)
        ));
    }
}
