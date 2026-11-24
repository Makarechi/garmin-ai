use std::time::Duration;

use ga_db::{
    Database,
    delivery::DeliveryState,
    outbox_claims::{
        ReplyClaim, ReplyDestination, ReplyDispatchError, ReplyDispatchResult, ReplyObservation,
        ReplySendOutcome,
    },
    share_policy::ShareRequirement,
};
use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use time::OffsetDateTime;
use tokio::sync::watch;

use super::{TelegramSendOutcome, TelegramTransport};
use crate::{
    garmin_worker::GarminReauthNoticeSettings, telegram_input::TelegramBinding,
    telegram_reply::TelegramReplyError,
};

// The Python delivery worker uses the same fallback when no retry time is provided.
const UNSUPPORTED_RETRY: Duration = Duration::from_mins(15);

impl TelegramTransport {
    /// Send an ordinary neutral reply inside its revalidated database delivery fence.
    pub async fn dispatch_claimed_reply(
        &self,
        database: &Database,
        binding: TelegramBinding<'_>,
        claim: ReplyClaim,
        requirements: &[ShareRequirement<'_>],
    ) -> Result<ReplyDispatchResult, ReplyDispatchError> {
        self.dispatch_runtime_reply(database, binding, claim, requirements, None)
            .await
    }

    pub(in crate::telegram_transport) async fn dispatch_runtime_reply(
        &self,
        database: &Database,
        binding: TelegramBinding<'_>,
        claim: ReplyClaim,
        requirements: &[ShareRequirement<'_>],
        garmin_notice: Option<&GarminReauthNoticeSettings>,
    ) -> Result<ReplyDispatchResult, ReplyDispatchError> {
        let destination = ReplyDestination {
            owner_id: binding.owner_id,
            channel: "telegram",
            instance_id: binding.instance_id,
        };
        database
            .dispatch_telegram_runtime_reply(
                destination,
                binding.external_owner_id,
                claim,
                requirements,
                |mut body, source| async move {
                    let launch = garmin_notice
                        .filter(|notice| source.as_deref() == Some(notice.source.as_str()))
                        .and_then(|notice| notice.launch.as_ref());
                    if launch.is_some() {
                        body["blocks"] = serde_json::json!([{"text":"Garmin требует повторного входа. Откройте защищённую форму и введите почтовый код подтверждения. История и дневник доступны."}]);
                    }
                    let intent = OutboundIntent::normalize(body).ok()?;
                    let started = clock()?;
                    let result = self.send_reply(binding, &intent, started, launch).await;
                    observe_delivery(result, clock()?)
                },
            )
            .await
    }

    pub(in crate::telegram_transport) async fn dispatch_tracker_checkin(
        &self,
        database: &Database,
        binding: TelegramBinding<'_>,
        claim: ReplyClaim,
        ready: &watch::Receiver<bool>,
    ) -> Result<ReplyDispatchResult, ReplyDispatchError> {
        let destination = ReplyDestination {
            owner_id: binding.owner_id,
            channel: "telegram",
            instance_id: binding.instance_id,
        };
        database
            .dispatch_telegram_tracker_checkin(
                destination,
                binding.external_owner_id,
                claim,
                |body| async move {
                    let now = clock()?;
                    if !*ready.borrow() {
                        return retry_after(now, Duration::from_secs(2))
                            .map(ReplySendOutcome::RetryAt);
                    }
                    let intent = OutboundIntent::normalize(body).ok()?;
                    let result = self.send_reply(binding, &intent, now, None).await;
                    observe_delivery(result, clock()?)
                },
            )
            .await
    }
}

fn observe_delivery(
    result: Result<TelegramSendOutcome, TelegramReplyError>,
    observed_at: StoredInstant,
) -> Option<ReplySendOutcome> {
    let (state, provider_reference, detail) = match result {
        Ok(TelegramSendOutcome::Accepted {
            last_message_id, ..
        }) => (
            DeliveryState::ProviderAccepted,
            Some(last_message_id.to_string()),
            None,
        ),
        Ok(TelegramSendOutcome::RateLimited(delay)) => {
            return retry_after(observed_at, delay).map(ReplySendOutcome::RetryAt);
        }
        Ok(TelegramSendOutcome::Rejected) => (
            DeliveryState::Failed,
            None,
            Some("Telegram rejected the rendered message"),
        ),
        Ok(TelegramSendOutcome::Uncertain {
            last_message_id, ..
        }) => (
            DeliveryState::Uncertain,
            last_message_id.map(|id| id.to_string()),
            Some(if last_message_id.is_some() {
                "Telegram accepted only part of a multi-message intent"
            } else {
                "Telegram send outcome is unknown"
            }),
        ),
        Err(TelegramReplyError::Expired) => (DeliveryState::Expired, None, None),
        Err(TelegramReplyError::UnsupportedAttachments) => {
            return retry_after(observed_at, UNSUPPORTED_RETRY).map(ReplySendOutcome::RetryAt);
        }
        Err(TelegramReplyError::WrongBinding) => (DeliveryState::Cancelled, None, None),
        Err(TelegramReplyError::InvalidButton | TelegramReplyError::InvalidContent) => (
            DeliveryState::Failed,
            None,
            Some("Telegram reply could not be rendered"),
        ),
    };
    Some(ReplySendOutcome::Observation(ReplyObservation {
        state,
        observed_at,
        provider_reference,
        detail: detail.map(str::to_owned),
    }))
}

fn clock() -> Option<StoredInstant> {
    StoredInstant::from_unix_microseconds(
        i64::try_from(
            OffsetDateTime::now_utc()
                .unix_timestamp_nanos()
                .div_euclid(1_000),
        )
        .ok()?,
    )
    .ok()
}

fn retry_after(now: StoredInstant, delay: Duration) -> Option<StoredInstant> {
    let duration = i64::try_from(delay.as_micros()).ok()?;
    StoredInstant::from_unix_microseconds(now.unix_microseconds().checked_add(duration)?).ok()
}
