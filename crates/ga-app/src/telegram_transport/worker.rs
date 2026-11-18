use std::time::Duration;

use ga_db::{
    Database,
    jobs::telegram_inbound::{
        TelegramDiaryCommitError, TelegramInboundState, TelegramInboundTarget,
    },
    outbox_claims::ReplyDestination,
    outbox_enqueue::QueueIntentError,
    runtime_lease::{RuntimeLease, RuntimeLeaseError},
};
use ga_domain::timestamp::StoredInstant;
use tokio::{
    sync::watch,
    time::{Instant, sleep},
};

use super::{
    PollProgress, TelegramLoopError, TelegramTransport, TelegramTransportError, poller::wall_clock,
};
use crate::{
    garmin_worker::GarminReauthNoticeSettings, telegram_settings::TelegramIngressSettings,
};

const MODE_REFRESH: Duration = Duration::from_secs(30);
const WEBHOOK_BACKLOG_INTERVAL: Duration = Duration::from_secs(1);
const INACTIVE_RETRY: Duration = Duration::from_secs(2);
const EMPTY_POLL_RETRY: Duration = Duration::from_millis(250);
const ACK_IDLE: Duration = Duration::from_millis(250);
const CONTROL_LEASE_SECONDS: u32 = 5;
const HISTORY_LEASE_SECONDS: u32 = 10;
const DELIVERY_LEASE_SECONDS: u32 = 90;
const RECOVERY_INTERVAL: Duration = Duration::from_secs(10);
const NOTICE_INTERVAL: Duration = Duration::from_secs(2);
const SEND_RETRY_MICROSECONDS: i64 = 1_000_000;

impl TelegramTransport {
    /// Receive bursts in order on the session that owns the singleton worker lock.
    pub async fn run_receiver(
        &self,
        database: &Database,
        runtime: &mut RuntimeLease,
        settings: &TelegramIngressSettings,
    ) -> Result<(), RuntimeLeaseError> {
        self.receive(database, runtime, settings, None).await
    }

    pub async fn run_receiver_with_reminders(
        &self,
        database: &Database,
        runtime: &mut RuntimeLease,
        settings: &TelegramIngressSettings,
        reminders_ready: watch::Sender<bool>,
    ) -> Result<(), RuntimeLeaseError> {
        self.receive(database, runtime, settings, Some(reminders_ready))
            .await
    }

    async fn receive(
        &self,
        database: &Database,
        runtime: &mut RuntimeLease,
        settings: &TelegramIngressSettings,
        reminders_ready: Option<watch::Sender<bool>>,
    ) -> Result<(), RuntimeLeaseError> {
        let mut recovered = reminders_ready.is_none();
        let mut polling_until = None;
        let mut empty_webhook_reports = 0_u8;
        let mut progress = PollProgress::default();
        let set_ready = |ready| {
            if let Some(sender) = &reminders_ready {
                sender.send_if_modified(|current| {
                    let changed = *current != ready;
                    *current = ready;
                    changed
                });
            }
        };
        loop {
            if !recovered {
                recovered = runtime.recover_diary_question_deliveries().await.is_ok();
            }
            let binding = settings.confirmed_binding(database).await;
            let selected = runtime
                .telegram_channel_selected(settings.instance_id())
                .await;
            let (Ok(binding), Ok(true)) = (binding, selected) else {
                set_ready(false);
                polling_until = None;
                empty_webhook_reports = 0;
                progress = PollProgress::default();
                runtime.while_held(sleep(INACTIVE_RETRY)).await?;
                continue;
            };
            if polling_until.is_none_or(|expires| Instant::now() >= expires) {
                match runtime.while_held(self.webhook_backlog()).await? {
                    Ok(None) => {
                        if polling_until.is_none() {
                            set_ready(false);
                        }
                        polling_until = Some(Instant::now() + MODE_REFRESH);
                        empty_webhook_reports = 0;
                    }
                    Ok(Some(pending)) => {
                        polling_until = None;
                        progress = PollProgress::default();
                        empty_webhook_reports = if pending == 0 {
                            empty_webhook_reports.saturating_add(1)
                        } else {
                            0
                        };
                        set_ready(recovered && empty_webhook_reports >= 2);
                        let wait = if reminders_ready.is_some() {
                            WEBHOOK_BACKLOG_INTERVAL
                        } else {
                            MODE_REFRESH
                        };
                        runtime.while_held(sleep(wait)).await?;
                        continue;
                    }
                    Err(error) => {
                        set_ready(false);
                        polling_until = None;
                        empty_webhook_reports = 0;
                        progress = PollProgress::default();
                        runtime.while_held(sleep(retry_delay(error))).await?;
                        continue;
                    }
                }
            }
            let wait = match self.poll_once(runtime, binding, &mut progress).await {
                Ok(batch) => {
                    set_ready(recovered && batch.notification_ready);
                    if batch.updates > 0 {
                        continue;
                    }
                    EMPTY_POLL_RETRY
                }
                Err(TelegramLoopError::Runtime(error)) => return Err(error),
                Err(TelegramLoopError::Transport(error)) => {
                    set_ready(false);
                    polling_until = None;
                    retry_delay(error)
                }
                Err(_) => {
                    set_ready(false);
                    INACTIVE_RETRY
                }
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
        configured_timezone: &str,
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
                {
                    match database
                        .apply_telegram_owner_control(&lease, question_budget, configured_timezone)
                        .await
                    {
                        Ok(_) => continue,
                        Err(error) if error.is_busy() => {
                            if let Ok(now) = wall_clock() {
                                let _ = database.defer_telegram_owner_control(&lease, now).await;
                            }
                        }
                        Err(_) => {}
                    }
                }
            }
            sleep(ACK_IDLE).await;
        }
    }

    pub async fn run_history(&self, database: &Database, settings: &TelegramIngressSettings) -> ! {
        loop {
            if let (Ok(binding), Ok(now)) =
                (settings.confirmed_binding(database).await, wall_clock())
            {
                let target = TelegramInboundTarget {
                    owner_id: binding.owner_id,
                    instance_id: binding.instance_id,
                };
                if let Ok(Some(lease)) = database
                    .claim_local_telegram_history(target, now, HISTORY_LEASE_SECONDS)
                    .await
                {
                    match database.apply_telegram_history(&lease).await {
                        Ok(_) => continue,
                        Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy)) => {
                            if let Ok(now) = wall_clock() {
                                let _ = database.defer_telegram_owner_control(&lease, now).await;
                            }
                        }
                        Err(TelegramDiaryCommitError::UnsupportedMessage) => {
                            if let Ok(now) = wall_clock() {
                                let _ = database
                                    .finish_telegram_inbound(
                                        &lease,
                                        now,
                                        TelegramInboundState::Invalid,
                                    )
                                    .await;
                            }
                        }
                        Err(_) => {}
                    }
                }
            }
            sleep(ACK_IDLE).await;
        }
    }

    /// Deliver durable ordinary replies and hold unknown provider outcomes for reconciliation.
    pub async fn run_replies(&self, database: &Database, settings: &TelegramIngressSettings) -> ! {
        self.run_replies_with_garmin_notices(database, settings, None)
            .await
    }

    pub async fn run_replies_with_garmin_notices(
        &self,
        database: &Database,
        settings: &TelegramIngressSettings,
        garmin_notice: Option<&GarminReauthNoticeSettings>,
    ) -> ! {
        let mut recover_at = Instant::now();
        let mut notice_at = Instant::now();
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
                if Instant::now() >= notice_at {
                    let _ = database
                        .queue_telegram_storage_notice(
                            binding.owner_id,
                            binding.instance_id,
                            binding.external_owner_id,
                            now,
                        )
                        .await;
                    if let Some(notice) = garmin_notice {
                        let _ = database
                            .queue_telegram_garmin_notice(
                                binding.owner_id,
                                binding.instance_id,
                                binding.external_owner_id,
                                &notice.source,
                                now,
                            )
                            .await;
                    }
                    notice_at = Instant::now() + NOTICE_INTERVAL;
                }
                if let Ok(Some(claim)) = database
                    .claim_outbox_reply(destination, now, DELIVERY_LEASE_SECONDS)
                    .await
                {
                    if self
                        .dispatch_runtime_reply(database, binding, claim, &[], garmin_notice)
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
