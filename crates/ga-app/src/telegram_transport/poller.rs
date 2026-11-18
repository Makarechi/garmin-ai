use std::{error::Error, fmt, time::Duration};

use ga_db::{
    Database,
    channel_ingress::telegram_poll::TelegramPollError,
    jobs::{
        FailureReason, JobOutcome, JobStoreError, RecordedJobStatus,
        telegram_ack::{TelegramAckAuthorization, TelegramAckTarget},
    },
    runtime_lease::{RuntimeLease, RuntimeLeaseError},
    telegram_cursor::TelegramCursorError,
};
use ga_domain::timestamp::StoredInstant;
use time::OffsetDateTime;
use tokio::time::Instant;

use super::{MAX_UPDATES, TelegramTransport, TelegramTransportError};
use crate::telegram_input::TelegramBinding;

const RECENT_POLL: Duration = Duration::from_secs(90);

#[derive(Default)]
pub struct PollProgress {
    caught_up_at: Option<Instant>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PollBatch {
    pub updates: usize,
    pub notification_ready: bool,
}

#[derive(Debug)]
pub enum TelegramLoopError {
    Clock,
    Runtime(RuntimeLeaseError),
    Cursor(TelegramCursorError),
    Transport(TelegramTransportError),
    Ingress(TelegramPollError),
    Acknowledgement(JobStoreError),
}

impl fmt::Display for TelegramLoopError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Clock => f.write_str("Telegram worker clock is unavailable"),
            Self::Runtime(error) => error.fmt(f),
            Self::Cursor(error) => error.fmt(f),
            Self::Transport(error) => error.fmt(f),
            Self::Ingress(error) => error.fmt(f),
            Self::Acknowledgement(error) => error.fmt(f),
        }
    }
}
impl Error for TelegramLoopError {}

impl TelegramTransport {
    /// Perform one poll cycle and commit each update before advancing the saved offset.
    pub async fn poll_once(
        &self,
        lease: &mut RuntimeLease,
        binding: TelegramBinding<'_>,
        progress: &mut PollProgress,
    ) -> Result<PollBatch, TelegramLoopError> {
        if !(1..(1_i64 << 52)).contains(&binding.external_owner_id) {
            return Err(TelegramLoopError::Ingress(
                TelegramPollError::InvalidBinding,
            ));
        }
        let result = self.poll_bound(lease, binding, progress).await;
        if result.is_err() {
            progress.caught_up_at = None;
        }
        result
    }

    async fn poll_bound(
        &self,
        lease: &mut RuntimeLease,
        binding: TelegramBinding<'_>,
        progress: &mut PollProgress,
    ) -> Result<PollBatch, TelegramLoopError> {
        lease
            .check_telegram_poll_pairing(
                binding.owner_id,
                binding.instance_id,
                binding.external_owner_id,
            )
            .await
            .map_err(TelegramLoopError::Ingress)?;
        let cursor = lease
            .owned_telegram_poll_cursor(binding.instance_id, wall_clock()?)
            .await
            .map_err(TelegramLoopError::Cursor)?;
        if cursor.reset {
            progress.caught_up_at = None;
        }
        let updates = lease
            .while_held(self.get_updates(cursor.offset))
            .await
            .map_err(TelegramLoopError::Runtime)?
            .map_err(TelegramLoopError::Transport)?;
        let received = wall_clock()?;
        let batch_at = Instant::now();
        let time_known = progress
            .caught_up_at
            .is_some_and(|at| batch_at.duration_since(at) < RECENT_POLL);
        let count = updates.len();
        for update in updates {
            binding
                .accept_paired_poll_update(lease, &update.body, received, time_known)
                .await
                .map_err(TelegramLoopError::Ingress)?;
        }
        // An empty response still crosses a request boundary after preflight.
        lease
            .check_telegram_poll_pairing(
                binding.owner_id,
                binding.instance_id,
                binding.external_owner_id,
            )
            .await
            .map_err(TelegramLoopError::Ingress)?;
        let caught_up = count < MAX_UPDATES;
        let selected = lease
            .telegram_channel_selected(binding.instance_id)
            .await
            .map_err(TelegramLoopError::Ingress)?;
        progress.caught_up_at = caught_up.then_some(batch_at);
        Ok(PollBatch {
            updates: count,
            notification_ready: caught_up && selected,
        })
    }

    /// Claim and finish one durable callback acknowledgement independently of the long poll.
    pub async fn acknowledge_once(
        &self,
        database: &Database,
        binding: TelegramBinding<'_>,
    ) -> Result<Option<RecordedJobStatus>, TelegramLoopError> {
        let target = TelegramAckTarget {
            owner_id: binding.owner_id,
            instance_id: binding.instance_id,
            confirmed_chat: Some(binding.external_owner_id),
        };
        let lease = database
            .claim_telegram_ack(target, wall_clock()?)
            .await
            .map_err(TelegramLoopError::Acknowledgement)?;
        let Some(lease) = lease else {
            return Ok(None);
        };
        let result = database
            .with_telegram_ack_authorization(
                &lease,
                target,
                self.acknowledge_callback(lease.callback_id()),
            )
            .await
            .map_err(TelegramLoopError::Acknowledgement)?;
        let now = wall_clock()?;
        let outcome = match result {
            TelegramAckAuthorization::BindingChanged => JobOutcome::Failed {
                reason: FailureReason::TelegramPairingChanged,
                retry_at: None,
            },
            TelegramAckAuthorization::Busy => JobOutcome::Failed {
                reason: FailureReason::TelegramPolicyBusy,
                retry_at: StoredInstant::from_unix_microseconds(
                    now.unix_microseconds().saturating_add(250_000),
                )
                .ok(),
            },
            TelegramAckAuthorization::Observed(Ok(()) | Err(TelegramTransportError::Rejected)) => {
                JobOutcome::Completed
            }
            TelegramAckAuthorization::Observed(Err(TelegramTransportError::RateLimited(delay))) => {
                let micros = i64::try_from(delay.as_micros())
                    .ok()
                    .and_then(|delay| now.unix_microseconds().checked_add(delay));
                let retry_at =
                    micros.and_then(|micros| StoredInstant::from_unix_microseconds(micros).ok());
                JobOutcome::Failed {
                    reason: if retry_at.is_some() {
                        FailureReason::RuntimeError
                    } else {
                        FailureReason::ProviderRequestInvalid
                    },
                    retry_at,
                }
            }
            TelegramAckAuthorization::Observed(Err(
                TelegramTransportError::Authorization
                | TelegramTransportError::InvalidInput
                | TelegramTransportError::Configuration,
            )) => JobOutcome::Failed {
                reason: FailureReason::ProviderRequestInvalid,
                retry_at: None,
            },
            TelegramAckAuthorization::Observed(Err(_)) => JobOutcome::Failed {
                reason: FailureReason::RuntimeError,
                retry_at: None,
            },
        };
        let status = database
            .finish_telegram_ack(&lease, now, outcome)
            .await
            .map_err(TelegramLoopError::Acknowledgement)?;
        Ok(Some(status))
    }
}

pub(super) fn wall_clock() -> Result<StoredInstant, TelegramLoopError> {
    let micros = i64::try_from(
        OffsetDateTime::now_utc()
            .unix_timestamp_nanos()
            .div_euclid(1_000),
    )
    .map_err(|_| TelegramLoopError::Clock)?;
    StoredInstant::from_unix_microseconds(micros).map_err(|_| TelegramLoopError::Clock)
}
