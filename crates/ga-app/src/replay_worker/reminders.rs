use std::time::Duration;

use ga_app::telegram_transport::DiaryReminderSettings;
use ga_db::{Database, outbox_claims::ReplyDestination};
use ga_domain::event_payload::legacy_boolean;
use time::Time;

use super::clock;
use crate::configuration;

pub(super) fn settings(timezone: &str) -> Result<DiaryReminderSettings, &'static str> {
    let invalid = "Reminder budget or quiet hours are invalid";
    let [budget, enabled, quiet_start, quiet_end] = configuration::read([
        "GA_QUESTION_BUDGET",
        "GA_PROACTIVE_ENABLED",
        "GA_QUIET_START_HOUR",
        "GA_QUIET_END_HOUR",
    ])
    .map_err(|_| invalid)?;
    let daily_budget = budget
        .as_deref()
        .unwrap_or("2")
        .parse::<u8>()
        .ok()
        .filter(|value| *value <= 20)
        .ok_or(invalid)?;
    let enabled = enabled
        .map_or(Some(false), |value| {
            legacy_boolean(&serde_json::Value::String(value))
        })
        .ok_or(invalid)?;
    let quiet = |hour: Option<&str>, fallback: &str| {
        hour.unwrap_or(fallback)
            .parse::<u8>()
            .ok()
            .and_then(|hour| Time::from_hms(hour, 0, 0).ok())
            .ok_or(invalid)
    };
    Ok(DiaryReminderSettings {
        timezone: timezone.to_owned(),
        enabled,
        daily_budget,
        quiet_start: quiet(quiet_start.as_deref(), "22")?,
        quiet_end: quiet(quiet_end.as_deref(), "8")?,
    })
}

pub(super) async fn run_web(database: &Database, settings: &DiaryReminderSettings) -> ! {
    let policy = settings.policy();
    let mut after = None;
    loop {
        let delay = if let (Ok(owner), Ok(now)) = (database.enrolled_owner_id().await, clock()) {
            let destination = ReplyDestination {
                owner_id: owner,
                channel: "web",
                instance_id: "local",
            };
            match database
                .queue_channel_tracker_checkins(destination, &policy, now, after)
                .await
            {
                Ok(page) => {
                    after = page.next;
                    if after.is_none() { 15 } else { 2 }
                }
                Err(_) => 2,
            }
        } else {
            15
        };
        tokio::time::sleep(Duration::from_secs(delay)).await;
    }
}
