use std::{future, time::Duration};

use ga_db::{
    Database,
    outbox_claims::ReplyDestination,
    proactive_control::{
        DiaryQuestionOutcome, NotificationExclusions, NotificationPolicy, ProactiveControlError,
        ReservedDiaryQuestion,
    },
};
use ga_domain::{
    channel::{OutboundIntent, telegram_conversation_id},
    timestamp::StoredInstant,
};
use serde_json::json;
use time::Time;
use tokio::{sync::watch, time::sleep};

use super::{TelegramSendOutcome, TelegramTransport, poller::wall_clock};
use crate::{telegram_input::TelegramBinding, telegram_settings::TelegramIngressSettings};

const CHECK_INTERVAL: Duration = Duration::from_secs(15);
const RETRY_INTERVAL: Duration = Duration::from_secs(2);

pub struct DiaryReminderSettings {
    pub timezone: String,
    pub enabled: bool,
    pub daily_budget: u8,
    pub quiet_start: Time,
    pub quiet_end: Time,
}

impl TelegramTransport {
    /// Process the diary reminder queue while acknowledgement and control lanes remain independent.
    pub async fn run_diary_reminders(
        &self,
        database: &Database,
        settings: &TelegramIngressSettings,
        configured: &DiaryReminderSettings,
        mut ready: watch::Receiver<bool>,
    ) -> ! {
        let policy = NotificationPolicy {
            timezone: &configured.timezone,
            default_enabled: configured.enabled,
            daily_budget: Some(configured.daily_budget),
            quiet_start: configured.quiet_start,
            quiet_end: configured.quiet_end,
            snoozed_until: None,
            destination: None,
            exclusions: NotificationExclusions::default(),
        };
        let mut current = None;
        loop {
            if ready.wait_for(|ready| *ready).await.is_err() {
                future::pending::<()>().await;
            }
            let wait = if let Ok(binding) = settings.confirmed_binding(database).await {
                match self
                    .reminder_step(database, binding, &policy, &ready, &mut current)
                    .await
                {
                    Ok(true) => continue,
                    Ok(false) => CHECK_INTERVAL,
                    Err(_) => RETRY_INTERVAL,
                }
            } else {
                RETRY_INTERVAL
            };
            sleep(wait).await;
        }
    }

    async fn reminder_step(
        &self,
        database: &Database,
        binding: TelegramBinding<'_>,
        policy: &NotificationPolicy<'_>,
        ready: &watch::Receiver<bool>,
        current: &mut Option<ReservedDiaryQuestion>,
    ) -> Result<bool, ProactiveControlError> {
        let clock = || wall_clock().map_err(|_| ProactiveControlError::InvalidCommand);
        if current.is_none() {
            if !database
                .telegram_diary_reminders_enabled(
                    binding.owner_id,
                    binding.instance_id,
                    policy.default_enabled,
                )
                .await?
            {
                return Ok(false);
            }
            let now = clock()?;
            database
                .generate_diary_questions(binding.owner_id, now, policy.timezone)
                .await?;
            *current = database
                .reserve_diary_question(binding.owner_id, policy, now)
                .await?;
        }
        let Some(question) = current.as_ref() else {
            return Ok(false);
        };
        let now = clock()?;
        let destination = ReplyDestination {
            owner_id: binding.owner_id,
            channel: "telegram",
            instance_id: binding.instance_id,
        };
        database
            .dispatch_diary_question(
                destination,
                binding.external_owner_id,
                question,
                policy,
                now,
                |text| async move {
                    let allowed = database
                        .telegram_diary_reminders_enabled(
                            binding.owner_id,
                            binding.instance_id,
                            policy.default_enabled,
                        )
                        .await
                        .unwrap_or(false);
                    if !allowed || !*ready.borrow() {
                        return retry_outcome(now, RETRY_INTERVAL);
                    }
                    self.send_diary_reminder(binding, now, text).await
                },
            )
            .await?;
        // On a database error preserve the same reservation. Delivery never starts without its marker.
        *current = None;
        Ok(true)
    }

    async fn send_diary_reminder(
        &self,
        binding: TelegramBinding<'_>,
        now: StoredInstant,
        text: String,
    ) -> DiaryQuestionOutcome {
        let intent = OutboundIntent::normalize(json!({
            "owner_id": binding.owner_id.to_string(),
            "conversation_id": telegram_conversation_id(
                binding.owner_id, binding.instance_id, binding.external_owner_id,
            ).to_string(),
            "channel_instance": {"channel": "telegram", "instance_id": binding.instance_id},
            "blocks": [{"text": text}],
            "initiative": true,
        }));
        let Ok(intent) = intent else {
            return DiaryQuestionOutcome::Uncertain;
        };
        match self.send_neutral_reply(binding, &intent, now).await {
            Ok(TelegramSendOutcome::Accepted {
                last_message_id, ..
            }) => DiaryQuestionOutcome::Accepted(last_message_id),
            Ok(TelegramSendOutcome::RateLimited(delay)) => {
                retry_outcome(wall_clock().unwrap_or(now), delay)
            }
            _ => DiaryQuestionOutcome::Uncertain,
        }
    }
}

fn retry_outcome(at: StoredInstant, delay: Duration) -> DiaryQuestionOutcome {
    i64::try_from(delay.as_micros())
        .ok()
        .and_then(|delay| at.unix_microseconds().checked_add(delay))
        .and_then(|retry| StoredInstant::from_unix_microseconds(retry).ok())
        .map_or(
            DiaryQuestionOutcome::Uncertain,
            DiaryQuestionOutcome::RetryAt,
        )
}
