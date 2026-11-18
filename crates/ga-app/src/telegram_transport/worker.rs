use std::time::Duration;

use ga_db::{
    Database,
    runtime_lease::{RuntimeLease, RuntimeLeaseError},
};
use tokio::time::{Instant, sleep};

use super::{PollProgress, TelegramLoopError, TelegramTransport, TelegramTransportError};
use crate::telegram_settings::TelegramIngressSettings;

const MODE_REFRESH: Duration = Duration::from_secs(30);
const INACTIVE_RETRY: Duration = Duration::from_secs(2);
const EMPTY_POLL_RETRY: Duration = Duration::from_millis(250);
const ACK_IDLE: Duration = Duration::from_millis(250);

impl TelegramTransport {
    /// Receive bursts in order on the session that owns the singleton worker lock.
    pub async fn run_receiver(
        &self,
        database: &Database,
        runtime: &mut RuntimeLease,
        settings: &TelegramIngressSettings,
    ) -> Result<(), RuntimeLeaseError> {
        let mut mode = None;
        let mut progress = PollProgress::default();
        loop {
            let binding = settings.confirmed_binding(database).await;
            let selected = runtime
                .telegram_channel_selected(settings.instance_id())
                .await;
            let (Ok(binding), Ok(true)) = (binding, selected) else {
                mode = None;
                progress = PollProgress::default();
                runtime.while_held(sleep(INACTIVE_RETRY)).await?;
                continue;
            };
            if mode.is_none_or(|(_, expires)| Instant::now() >= expires) {
                match runtime.while_held(self.webhook_active()).await? {
                    Ok(active) => mode = Some((active, Instant::now() + MODE_REFRESH)),
                    Err(error) => {
                        progress = PollProgress::default();
                        runtime.while_held(sleep(retry_delay(error))).await?;
                        continue;
                    }
                }
            }
            if let Some((true, expires)) = mode {
                progress = PollProgress::default();
                runtime
                    .while_held(tokio::time::sleep_until(expires))
                    .await?;
                continue;
            }
            let wait = match self.poll_once(runtime, binding, &mut progress).await {
                Ok(batch) if batch.updates > 0 => continue,
                Ok(_) => EMPTY_POLL_RETRY,
                Err(TelegramLoopError::Runtime(error)) => return Err(error),
                Err(TelegramLoopError::Transport(error)) => {
                    mode = None;
                    retry_delay(error)
                }
                Err(_) => INACTIVE_RETRY,
            };
            runtime.while_held(sleep(wait)).await?;
        }
    }

    /// Confirm already-durable callback updates independently of long polling.
    pub async fn run_acknowledgements(
        &self,
        database: &Database,
        settings: &TelegramIngressSettings,
    ) -> ! {
        loop {
            let wait = match settings.confirmed_binding(database).await {
                Ok(binding) => match self.acknowledge_once(database, binding).await {
                    Ok(Some(_)) => continue,
                    Ok(None) => ACK_IDLE,
                    Err(_) => INACTIVE_RETRY,
                },
                Err(_) => INACTIVE_RETRY,
            };
            sleep(wait).await;
        }
    }
}

fn retry_delay(error: TelegramTransportError) -> Duration {
    match error {
        TelegramTransportError::RateLimited(delay) => delay.max(EMPTY_POLL_RETRY),
        _ => INACTIVE_RETRY,
    }
}
