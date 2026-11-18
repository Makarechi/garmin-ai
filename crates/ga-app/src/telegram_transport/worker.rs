use std::time::Duration;

use ga_db::{
    Database,
    jobs::telegram_inbound::TelegramInboundTarget,
    outbox_claims::ReplyDestination,
    runtime_lease::{RuntimeLease, RuntimeLeaseError},
};
use ga_domain::timestamp::StoredInstant;
use tokio::time::{Instant, sleep};

use super::{
    PollProgress, TelegramLoopError, TelegramTransport, TelegramTransportError, poller::wall_clock,
};
use crate::telegram_settings::TelegramIngressSettings;

const MODE_REFRESH: Duration = Duration::from_secs(30);
const INACTIVE_RETRY: Duration = Duration::from_secs(2);
const EMPTY_POLL_RETRY: Duration = Duration::from_millis(250);
const ACK_IDLE: Duration = Duration::from_millis(250);
const CONTROL_LEASE_SECONDS: u32 = 5;
const DELIVERY_LEASE_SECONDS: u32 = 90;
const RECOVERY_INTERVAL: Duration = Duration::from_secs(10);
const SEND_RETRY_MICROSECONDS: i64 = 1_000_000;

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

    /// Handle local commands on a separate lane while external analysis or polling waits.
    pub async fn run_owner_controls(
        &self,
        database: &Database,
        settings: &TelegramIngressSettings,
        question_budget: u16,
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
                    .claim_local_telegram_control(target, now, CONTROL_LEASE_SECONDS)
                    .await
                    && database
                        .apply_telegram_owner_control(&lease, question_budget)
                        .await
                        .is_ok()
                {
                    continue;
                }
            }
            sleep(ACK_IDLE).await;
        }
    }

    /// Deliver durable ordinary replies and hold unknown provider outcomes for reconciliation.
    pub async fn run_replies(&self, database: &Database, settings: &TelegramIngressSettings) -> ! {
        let mut recover_at = Instant::now();
        loop {
            if let (Ok(binding), Ok(now)) =
                (settings.confirmed_binding(database).await, wall_clock())
            {
                let destination = ReplyDestination {
                    owner_id: binding.owner_id,
                    channel: "telegram",
                    instance_id: binding.instance_id,
                };
                if Instant::now() >= recover_at {
                    let _ = database
                        .recover_expired_telegram_outbox(&destination, now)
                        .await;
                    recover_at = Instant::now() + RECOVERY_INTERVAL;
                }
                if let Ok(Some(claim)) = database
                    .claim_outbox_reply(destination, now, DELIVERY_LEASE_SECONDS)
                    .await
                {
                    if self
                        .dispatch_claimed_reply(database, binding, claim, &[])
                        .await
                        .is_ok()
                    {
                        continue;
                    }
                    if let Ok(observed) = wall_clock()
                        && let Ok(retry_at) = StoredInstant::from_unix_microseconds(
                            observed
                                .unix_microseconds()
                                .saturating_add(SEND_RETRY_MICROSECONDS),
                        )
                    {
                        let _ = database
                            .defer_unsent_reply(binding.owner_id, claim, retry_at)
                            .await;
                    }
                }
            }
            sleep(ACK_IDLE).await;
        }
    }
}

fn retry_delay(error: TelegramTransportError) -> Duration {
    match error {
        TelegramTransportError::RateLimited(delay) => delay.max(EMPTY_POLL_RETRY),
        _ => INACTIVE_RETRY,
    }
}
