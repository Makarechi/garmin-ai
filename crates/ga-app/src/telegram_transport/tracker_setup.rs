use std::time::Duration;

use ga_db::{Database, jobs::telegram_inbound::TelegramInboundTarget};
use tokio::time::sleep;

use super::{TelegramTransport, local_text::complete_or_retry, poller::wall_clock};
use crate::telegram_settings::TelegramIngressSettings;

impl TelegramTransport {
    /// Serve the ordered setup conversation using the local durable diary store.
    pub async fn run_tracker_setup(
        &self,
        database: &Database,
        settings: &TelegramIngressSettings,
        timezone: &str,
        locale: &str,
        transcribe_voice: bool,
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
                    .claim_telegram_tracker_setup(target, now, 10, transcribe_voice)
                    .await
                {
                    let result = database
                        .apply_telegram_tracker_setup(&lease, timezone, locale)
                        .await;
                    if complete_or_retry(database, &lease, result).await {
                        continue;
                    }
                }
            }
            sleep(Duration::from_millis(250)).await;
        }
    }
}
