//! Transactional queue notifications are hints; durable rows and leases remain authoritative.

use std::{future::pending, sync::Arc, time::Duration};

use sqlx::{PgPool, postgres::PgListener};
use sqlx_postgres::{PgConnectOptions, PgPoolOptions};
use tokio::{
    sync::{Mutex, watch},
    time::{Instant, MissedTickBehavior, interval_at, sleep, sleep_until, timeout},
};

use crate::{DATABASE_CONNECT_TIMEOUT, Database};

const CHANNEL: &str = "ga_work_ready";
const RECOVERY_INTERVAL: Duration = Duration::from_secs(30);
const REFRESH_COALESCE: Duration = Duration::from_millis(25);
const DEADLINE_QUERY_TIMEOUT: Duration = Duration::from_millis(1500);
const TOPIC_COUNT: usize = 10;

/// Independent consumers of durable work. A signal contains no user data.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(usize)]
pub enum WorkTopic {
    TelegramInbound,
    TelegramAcknowledgements,
    TelegramOutbox,
    Garmin,
    GarminSchedule,
    Replay,
    Maintenance,
    Reminders,
    Trends,
    Extensions,
}

impl WorkTopic {
    const ALL: [Self; TOPIC_COUNT] = [
        Self::TelegramInbound,
        Self::TelegramAcknowledgements,
        Self::TelegramOutbox,
        Self::Garmin,
        Self::GarminSchedule,
        Self::Replay,
        Self::Maintenance,
        Self::Reminders,
        Self::Trends,
        Self::Extensions,
    ];

    fn from_payload(payload: &str) -> Option<Self> {
        match payload {
            "telegram_inbound" => Some(Self::TelegramInbound),
            "telegram_acknowledgements" => Some(Self::TelegramAcknowledgements),
            "telegram_outbox" => Some(Self::TelegramOutbox),
            "garmin" => Some(Self::Garmin),
            "garmin_schedule" => Some(Self::GarminSchedule),
            "replay" => Some(Self::Replay),
            "maintenance" => Some(Self::Maintenance),
            "reminders" => Some(Self::Reminders),
            "trends" => Some(Self::Trends),
            "extensions" => Some(Self::Extensions),
            _ => None,
        }
    }

    fn has_deadlines(self) -> bool {
        !matches!(self, Self::GarminSchedule | Self::Trends)
    }
}

/// Subscribe before checking the queue, then wait only after a check finds no work.
/// Retained generations close the gap between that empty check and sleeping.
pub struct WorkSubscription {
    generation: watch::Receiver<u64>,
}

impl WorkSubscription {
    /// Consume a retained hint without blocking, for workers draining a busy queue.
    pub fn take_pending(&mut self) -> bool {
        if self.generation.has_changed().unwrap_or(false) {
            self.generation.borrow_and_update();
            true
        } else {
            false
        }
    }

    /// Returns true for a notification and false when the maximum wait expires.
    /// The maximum also bounds recovery if no listener has been started.
    pub async fn wait(&mut self, maximum: Duration) -> bool {
        if self.take_pending() {
            return true;
        }
        match timeout(maximum, self.generation.changed()).await {
            Ok(Ok(())) => true,
            Ok(Err(_)) => {
                // A subscription can outlive its Database. Do not busy-loop then.
                sleep(maximum).await;
                false
            }
            Err(_) => false,
        }
    }
}

pub(crate) struct WorkWakeups {
    pool: PgPool,
    generations: [watch::Sender<u64>; TOPIC_COUNT],
    running: Mutex<()>,
}

impl WorkWakeups {
    pub(crate) fn new(options: PgConnectOptions) -> Arc<Self> {
        Arc::new(Self {
            pool: PgPoolOptions::new()
                .max_connections(1)
                .min_connections(0)
                .acquire_timeout(DATABASE_CONNECT_TIMEOUT)
                .connect_lazy_with(
                    options
                        .application_name("garmin-ai-work-wakeups")
                        .options([("statement_timeout", "750ms")]),
                ),
            generations: std::array::from_fn(|_| watch::channel(0).0),
            running: Mutex::new(()),
        })
    }

    fn notify(&self, topic: WorkTopic) {
        self.generations[topic as usize].send_modify(|generation| {
            *generation = generation.wrapping_add(1);
        });
    }

    fn notify_all(&self) {
        for topic in WorkTopic::ALL {
            self.notify(topic);
        }
    }

    async fn run(&self) -> ! {
        // Database clones share one listener. Cancellation releases this guard.
        let _running = self.running.lock().await;
        let mut recovery = interval_at(Instant::now() + RECOVERY_INTERVAL, RECOVERY_INTERVAL);
        recovery.set_missed_tick_behavior(MissedTickBehavior::Skip);
        let mut retry = Duration::from_secs(1);
        loop {
            let connect = async {
                let mut listener = PgListener::connect_with(&self.pool).await?;
                listener.listen(CHANNEL).await?;
                Ok::<_, sqlx::Error>(listener)
            };
            let listener = tokio::select! {
                result = connect => result,
                _ = recovery.tick() => {
                    self.notify_all();
                    continue;
                }
            };
            if let Ok(mut listener) = listener {
                let connected_at = Instant::now();
                // LISTEN is established before the durable queue scan. Both the
                // first connection and a replacement recover missed commits.
                self.notify_all();
                self.listen(&mut listener, &mut recovery).await;
                // A successful TCP connection followed by another failed queue
                // query is not recovery. Retain backoff across short sessions.
                if connected_at.elapsed() >= RECOVERY_INTERVAL {
                    retry = Duration::from_secs(1);
                }
            }
            eprintln!(
                "Background work notifications unavailable; retrying in {} seconds",
                retry.as_secs()
            );
            tokio::select! {
                () = sleep(retry) => {}
                _ = recovery.tick() => self.notify_all(),
            }
            retry = (retry * 2).min(RECOVERY_INTERVAL);
        }
    }

    async fn listen(&self, listener: &mut PgListener, recovery: &mut tokio::time::Interval) {
        let mut deadlines: Vec<(WorkTopic, Instant)> = Vec::new();
        let mut refresh_at = Some(Instant::now());
        let mut after_refresh = std::array::from_fn(|index| WorkTopic::ALL[index].has_deadlines());
        loop {
            let next_due = deadlines.iter().map(|(_, at)| *at).min();
            tokio::select! {
                notification = listener.try_recv() => {
                    match notification {
                        Ok(Some(notification)) => {
                            let mut signalled = [false; TOPIC_COUNT];
                            let mut changed = self.observe(notification.payload(), &mut signalled, &mut after_refresh);
                            // Deliver hints before querying deadlines and coalesce a
                            // burst without starving timers or shutdown cancellation.
                            for _ in 0..127 {
                                let Some(notification) = listener.next_buffered() else { break };
                                changed |= self.observe(notification.payload(), &mut signalled, &mut after_refresh);
                            }
                            if changed {
                                refresh_at.get_or_insert(Instant::now() + REFRESH_COALESCE);
                            }
                        }
                        Ok(None) => {
                            // try_recv eagerly reconnects and re-LISTENs before
                            // reporting loss; recv would silently hide this gap.
                            self.notify_all();
                            refresh_at = Some(Instant::now());
                            after_refresh = std::array::from_fn(|index| WorkTopic::ALL[index].has_deadlines());
                        }
                        Err(_) => return,
                    }
                }
                () = wait_until(refresh_at) => {
                    let Ok(next) = future_deadlines(listener).await else { return };
                    // The snapshot excludes due rows. Wake timers that expired
                    // during this refresh even if another topic triggered it.
                    let now = Instant::now();
                    for &(topic, at) in &deadlines {
                        after_refresh[topic as usize] |= at <= now;
                    }
                    deadlines = next;
                    refresh_at = None;
                    // A near-future retry may become due between the immediate
                    // hint and the snapshot. Repeat each affected topic once;
                    // already-due rows then cannot create a recurring timer.
                    for topic in WorkTopic::ALL {
                        if after_refresh[topic as usize] {
                            self.notify(topic);
                        }
                    }
                    after_refresh.fill(false);
                }
                () = wait_until(next_due) => {
                    let now = Instant::now();
                    deadlines.retain(|(topic, at)| {
                        if *at <= now {
                            self.notify(*topic);
                            // A second deadline of this topic may cross while
                            // refreshing the first one, without another commit.
                            after_refresh[*topic as usize] = true;
                            false
                        } else {
                            true
                        }
                    });
                    refresh_at.get_or_insert(now + REFRESH_COALESCE);
                }
                _ = recovery.tick() => {
                    self.notify_all();
                    refresh_at = Some(Instant::now());
                    after_refresh = std::array::from_fn(|index| WorkTopic::ALL[index].has_deadlines());
                }
            }
        }
    }

    fn observe(
        &self,
        payload: &str,
        signalled: &mut [bool; TOPIC_COUNT],
        after_refresh: &mut [bool; TOPIC_COUNT],
    ) -> bool {
        if payload == "deadlines" {
            return true;
        }
        let Some(topic) = WorkTopic::from_payload(payload) else {
            return false;
        };
        if !signalled[topic as usize] {
            self.notify(topic);
            signalled[topic as usize] = true;
        }
        after_refresh[topic as usize] |= topic.has_deadlines();
        true
    }
}

impl Database {
    #[must_use]
    pub fn work_subscription(&self, topic: WorkTopic) -> WorkSubscription {
        WorkSubscription {
            generation: self.work_wakeups.generations[topic as usize].subscribe(),
        }
    }

    /// Run alongside worker tasks; cancellation stops the dedicated listener.
    pub async fn run_work_wakeups(&self) -> ! {
        self.work_wakeups.run().await
    }
}

async fn wait_until(deadline: Option<Instant>) {
    match deadline {
        Some(at) => sleep_until(at).await,
        None => pending::<()>().await,
    }
}

async fn future_deadlines(listener: &mut PgListener) -> Result<Vec<(WorkTopic, Instant)>, ()> {
    let rows: Vec<(String, i64)> = timeout(
        DEADLINE_QUERY_TIMEOUT,
        sqlx::query_as(include_str!("work_wakeups/deadlines.sql")).fetch_all(&mut *listener),
    )
    .await
    .map_err(|_| ())?
    .map_err(|_| ())?;
    let now = Instant::now();
    Ok(rows
        .into_iter()
        .filter_map(|(label, milliseconds)| {
            let topic = WorkTopic::from_payload(&label)?;
            let delay = Duration::from_millis(u64::try_from(milliseconds).ok()?);
            Some((topic, now + delay.min(RECOVERY_INTERVAL)))
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn notification_before_wait_is_retained_and_reaches_each_subscriber() {
        let (sender, receiver) = watch::channel(0);
        let mut first = WorkSubscription {
            generation: receiver,
        };
        let mut second = WorkSubscription {
            generation: sender.subscribe(),
        };
        sender.send_modify(|generation| *generation += 1);
        assert!(first.wait(Duration::ZERO).await);
        assert!(second.take_pending());
        assert!(!first.wait(Duration::ZERO).await);
        assert!(!second.take_pending());
    }

    #[tokio::test]
    async fn waiting_can_be_cancelled_without_consuming_a_later_notification() {
        let (sender, receiver) = watch::channel(0);
        let mut subscription = WorkSubscription {
            generation: receiver,
        };
        assert!(
            timeout(
                Duration::from_millis(1),
                subscription.wait(Duration::from_secs(1))
            )
            .await
            .is_err()
        );
        sender.send_modify(|generation| *generation += 1);
        assert!(subscription.wait(Duration::ZERO).await);
    }
}
