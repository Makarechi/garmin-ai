use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    error::Error,
    path::PathBuf,
    time::Duration,
};

use ga_db::{
    Database,
    outbox_claims::{ReplyDestination, ReplySendOutcome},
    proactive_control::{NotificationExclusions, NotificationPolicy},
};
use ga_domain::{event_payload::legacy_boolean, timestamp::StoredInstant};
use serde_json::{Map, Value, json};
use time::{OffsetDateTime, Time};
use tokio::time::{Instant, sleep};

use super::super::{Extension, ExtensionError, valid_secret_reference};
use crate::configuration;

const MAX_SETTINGS_BYTES: usize = 1024 * 1024;
const CHECK_INTERVAL: Duration = Duration::from_secs(15);
const RETRY_INTERVAL: Duration = Duration::from_secs(2);
const LEASE_SECONDS: u32 = 90;

struct ChannelSettings {
    id: String,
    provider: String,
    instance: String,
    executable: PathBuf,
    config: Map<String, Value>,
    secrets: BTreeMap<String, String>,
}

pub(crate) struct ScheduledChannelPlugins {
    channels: Vec<ChannelSettings>,
    daily_budget: u8,
    quiet_start: Time,
    quiet_end: Time,
}

impl ScheduledChannelPlugins {
    pub(crate) fn read() -> Result<Option<Self>, Box<dyn Error>> {
        let [configured, registry, budget, quiet_start, quiet_end] = configuration::read([
            "GA_INTEGRATIONS",
            "GA_CHANNEL_PLUGIN_EXECUTABLES",
            "GA_QUESTION_BUDGET",
            "GA_QUIET_START_HOUR",
            "GA_QUIET_END_HOUR",
        ])?;
        let Some(registry) = registry else {
            return Ok(None);
        };
        let invalid = || ExtensionError::ChannelConfiguration;
        let configured = configured.as_deref().unwrap_or("[]");
        if registry.len() > MAX_SETTINGS_BYTES || configured.len() > MAX_SETTINGS_BYTES {
            return Err(invalid().into());
        }
        let executables: BTreeMap<String, PathBuf> =
            serde_json::from_str(&registry).map_err(|_| invalid())?;
        let configured: Vec<Value> = serde_json::from_str(configured).map_err(|_| invalid())?;
        if executables.len() > 32 {
            return Err(invalid().into());
        }
        let mut channels = Vec::new();
        let mut routes = BTreeSet::new();
        for (id, executable) in executables {
            let mut matching = configured.iter().filter(|item| item["id"] == id);
            let Some(spec) = matching.next().filter(|item| item["kind"] == "channel") else {
                continue;
            };
            if matching.next().is_some() {
                return Err(invalid().into());
            }
            let enabled = spec
                .get("enabled")
                .map_or(Some(true), legacy_boolean)
                .ok_or_else(invalid)?;
            if !enabled {
                continue;
            }
            let provider = spec["provider"]
                .as_str()
                .filter(|name| valid_provider(name))
                .ok_or_else(invalid)?;
            if matches!(provider, "telegram" | "web") {
                continue;
            }
            let prefix = format!("channel:{provider}:");
            let instance = id.strip_prefix(&prefix).unwrap_or(&id);
            if instance.is_empty() || !routes.insert((provider.to_owned(), instance.to_owned())) {
                return Err(invalid().into());
            }
            Extension::new(&executable, &id).map_err(|_| invalid())?;
            let config = if spec["config"].is_null() {
                Map::new()
            } else {
                spec["config"].as_object().cloned().ok_or_else(invalid)?
            };
            let secrets: BTreeMap<String, String> = if spec["secret_refs"].is_null() {
                BTreeMap::new()
            } else {
                serde_json::from_value(spec["secret_refs"].clone()).map_err(|_| invalid())?
            };
            if secrets.len() > 16
                || !secrets
                    .iter()
                    .all(|(slot, reference)| valid_secret_reference(slot, reference))
            {
                return Err(invalid().into());
            }
            channels.push(ChannelSettings {
                provider: provider.into(),
                instance: instance.into(),
                id,
                config,
                secrets,
                executable,
            });
        }
        if channels.is_empty() {
            return Ok(None);
        }
        let hour = |value: Option<String>, fallback: u8| -> Result<Time, ExtensionError> {
            let hour = match value {
                Some(value) => value.parse::<u8>().map_err(|_| invalid())?,
                None => fallback,
            };
            Time::from_hms(hour, 0, 0).map_err(|_| invalid())
        };
        let daily_budget = budget
            .as_deref()
            .unwrap_or("2")
            .parse::<u8>()
            .ok()
            .filter(|limit| *limit <= 20)
            .ok_or_else(invalid)?;
        Ok(Some(Self {
            channels,
            daily_budget,
            quiet_start: hour(quiet_start, 22)?,
            quiet_end: hour(quiet_end, 8)?,
        }))
    }

    pub(crate) async fn run(&self, database: &Database) -> ! {
        let policy = NotificationPolicy {
            timezone: "UTC",
            default_enabled: true,
            daily_budget: Some(self.daily_budget),
            quiet_start: self.quiet_start,
            quiet_end: self.quiet_end,
            snoozed_until: None,
            destination: None,
            exclusions: NotificationExclusions::default(),
        };
        let mut next_scan = vec![(Instant::now(), None); self.channels.len()];
        loop {
            if let Ok(owner) = database.enrolled_owner_id().await {
                for (channel, (scan_at, after)) in self.channels.iter().zip(&mut next_scan) {
                    let Some(now) = clock() else { break };
                    let target = || ReplyDestination {
                        owner_id: owner,
                        channel: &channel.provider,
                        instance_id: &channel.instance,
                    };
                    if Instant::now() >= *scan_at {
                        let _ = database
                            .recover_expired_channel_outbox(&target(), now)
                            .await;
                        if let Ok(page) = database
                            .queue_channel_tracker_checkins(target(), &policy, now, *after)
                            .await
                        {
                            *after = page.next;
                            *scan_at = Instant::now()
                                + if after.is_none() {
                                    CHECK_INTERVAL
                                } else {
                                    RETRY_INTERVAL
                                };
                        }
                    }
                    if let Ok(Some(claim)) = database
                        .claim_channel_tracker_checkin(target(), now, LEASE_SECONDS)
                        .await
                        && database
                            .dispatch_channel_tracker_checkin(target(), claim, |intent| {
                                channel.deliver(intent, now)
                            })
                            .await
                            .is_err()
                        && let Some(retry) = retry_at(now)
                    {
                        let _ = database.defer_unsent_reply(owner, claim, retry).await;
                    }
                }
            }
            sleep(RETRY_INTERVAL).await;
        }
    }
}

impl ChannelSettings {
    async fn deliver(&self, intent: Value, now: StoredInstant) -> Option<ReplySendOutcome> {
        let mut extension = Extension::new(&self.executable, &self.id).ok()?;
        let secrets: Option<Map<String, Value>> = self
            .secrets
            .iter()
            .map(|(slot, reference)| {
                let value = env::var(reference).ok().filter(|value| !value.is_empty())?;
                Some((slot.clone(), Value::String(value)))
            })
            .collect();
        let Some(secrets) = secrets else {
            return retry_at(now).map(ReplySendOutcome::RetryAt);
        };
        extension
            .process
            .set_context(json!({"config":self.config,"secrets":secrets}));
        extension.deliver_checkin(intent, now).await
    }
}

fn valid_provider(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 100
        && bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(&byte)
        })
}

fn clock() -> Option<StoredInstant> {
    let epoch = i64::try_from(
        OffsetDateTime::now_utc()
            .unix_timestamp_nanos()
            .div_euclid(1_000),
    )
    .ok()?;
    StoredInstant::from_unix_microseconds(epoch).ok()
}

fn retry_at(now: StoredInstant) -> Option<StoredInstant> {
    let micros = i64::try_from(RETRY_INTERVAL.as_micros()).ok()?;
    StoredInstant::from_unix_microseconds(now.unix_microseconds().checked_add(micros)?).ok()
}
