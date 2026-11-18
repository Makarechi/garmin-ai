use std::{sync::OnceLock, time::Duration};

use fancy_regex::Regex;
use ga_db::{
    Database,
    jobs::telegram_inbound::{
        LocalTelegramText, TelegramDiaryCommitError, TelegramInboundLease, TelegramInboundState,
        TelegramInboundTarget, TelegramSafetyDecision,
    },
    outbox_enqueue::QueueIntentError,
};
use ga_domain::channel::InboundEnvelope;
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
        report_during_setup: explicit_report_while_drafting(text)?,
    })
}

fn explicit_report_while_drafting(text: &str) -> Result<bool, UrgentScreenUnavailable> {
    if obvious_third_party_emergency(text)? {
        return Ok(true);
    }
    static PERSONAL: OnceLock<Result<Regex, fancy_regex::Error>> = OnceLock::new();
    static ACUTE: OnceLock<Result<Regex, fancy_regex::Error>> = OnceLock::new();
    let personal = PERSONAL
        .get_or_init(|| {
            Regex::new(
                r"(?i)\b(?:i|my|me|we|our|я|мне|меня|мой|моя|моё|мои|нас|наш\w*|у него|у неё)\b",
            )
        })
        .as_ref()
        .map_err(|_| UrgentScreenUnavailable)?;
    if let Some((label, _)) = text.split_once('|') {
        return Ok(personal.is_match(label)? && obvious_urgent_symptoms(label)?);
    }
    let acute = ACUTE.get_or_init(|| Regex::new(
        r"(?i)\b(?:sudden|acute|внезапн\w*|резк\w*|(?:can't|cannot|can\s+not|unable\s+to|struggling\s+to)\s+breathe|не\s+могу\s+дышать)\b",
    )).as_ref().map_err(|_| UrgentScreenUnavailable)?;
    Ok(personal.is_match(text)? || acute.is_match(text)?)
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
