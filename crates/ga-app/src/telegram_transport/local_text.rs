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
use ga_domain::channel::InboundEnvelope;
use tokio::time::sleep;

use super::{TelegramTransport, poller::wall_clock};
use crate::{
    telegram_settings::TelegramIngressSettings,
    urgent_screen::{
        UrgentScreenUnavailable, decimal_digit, obvious_third_party_emergency,
        obvious_urgent_symptoms, urgent_notice,
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
    bounds: Regex,
}

impl SetupPatterns {
    fn build() -> Result<Self, fancy_regex::Error> {
        Ok(Self {
            label_is_report: Regex::new(
                r"(?i)(?:i|my|we|our|я|мне|меня|мой|моя|моё|мои|нас|наш\w*)",
            )?,
            tracker_title: Regex::new(
                r"(?i)(?:diary|tracker|journal|log|recovery|дневник|журнал|трекер|восстановление)",
            )?,
            title_personal: Regex::new(r"(?i)(?:i|my|у меня|я|мне|мой|моя|моё)")?,
            name_is_report: Regex::new(
                r"(?i)(?:i|my|me|he|she|they|someone|я|мне|у меня|у него|у неё)",
            )?,
            acute: Regex::new(r"(?i)(?:sudden|acute|внезапн\w*|резк\w*)")?,
            breathing: Regex::new(
                r"(?i)(?:can't|cannot|can\s+not|unable\s+to|struggling\s+to)\s+breathe|не\s+могу\s+дышать",
            )?,
            bounds: Regex::new(r"\A(-?\d+)\s*[-–]\s*(-?\d+)\z")?,
        })
    }

    fn field_label<'a>(&self, text: &'a str) -> Result<Option<&'a str>, UrgentScreenUnavailable> {
        let Some((label, specification)) = text.split_once('|') else {
            return Ok(None);
        };
        let label = label.trim();
        let specification = specification.trim();
        if !(1..=120).contains(&label.chars().count()) || specification.contains('|') {
            return Ok(None);
        }
        let end = specification
            .find(char::is_whitespace)
            .unwrap_or(specification.len());
        let kind = specification[..end].to_lowercase();
        let argument = specification[end..].trim();
        let accepted = match kind.as_str() {
            "text" | "текст" | "yes/no" | "да/нет" | "boolean" => argument.is_empty(),
            "choice" | "выбор" => {
                let mut options = std::collections::BTreeSet::new();
                argument.split(',').map(str::trim).all(|option| {
                    (1..=120).contains(&option.chars().count()) && options.insert(option)
                }) && options.len() <= 50
            }
            "scale" | "шкала" | "count" | "счётчик" | "счетчик" => {
                let bounds = self.bounds.captures(argument)?;
                let range = bounds.and_then(|bounds| {
                    Some((
                        parse_bound(bounds.get(1)?.as_str())?,
                        parse_bound(bounds.get(2)?.as_str())?,
                    ))
                });
                let scale = matches!(kind.as_str(), "scale" | "шкала");
                range.is_some_and(|(lower, upper)| {
                    lower <= upper
                        && if scale {
                            upper - lower <= 20 && format!("score_{lower}-{upper}").len() <= 32
                        } else {
                            lower >= 0
                        }
                })
            }
            _ => false,
        };
        Ok(accepted.then_some(label))
    }
}

fn parse_bound(value: &str) -> Option<i64> {
    if value.chars().count() > 100 {
        return None;
    }
    let negative = value.starts_with('-');
    let magnitude = value
        .trim_start_matches('-')
        .chars()
        .try_fold(0i64, |number, digit| {
            let next = number
                .checked_mul(10)?
                .checked_add(i64::from(decimal_digit(digit)?))?;
            (next <= (1i64 << 53)).then_some(next)
        })?;
    Some(if negative { -magnitude } else { magnitude })
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
    if let Some(label) = patterns.field_label(text)?
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

async fn complete_or_retry(
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
