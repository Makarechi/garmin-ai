use std::{
    collections::BTreeMap,
    env,
    error::Error,
    path::{Path, PathBuf},
    time::Duration,
};

use ga_db::{
    Database,
    jobs::{FailureReason, JobOutcome},
    source_pages::{PublicationState, SourcePoll},
};
use ga_domain::{
    event_payload::legacy_boolean,
    legacy_json::{Utf8Layout, encode},
    sha256_hex,
    timestamp::StoredInstant,
};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value, json};
use time::OffsetDateTime;
use tokio::{
    sync::watch,
    time::{Instant, sleep},
};

use super::{Extension, ExtensionError, run_until_shutdown};
use crate::configuration;

const MAX_PAGES: usize = 10;
const MAX_SETTINGS_BYTES: usize = 1024 * 1024;
const DEADLINE: Duration = Duration::from_secs(45);
const PLAN_INTERVAL: Duration = Duration::from_secs(30);
const JOB_INTERVAL: Duration = Duration::from_secs(1);

pub(crate) fn configured_source_fingerprint(input: Option<&str>, id: &str) -> Option<String> {
    SourceSettings::selected(input, id)
        .ok()??
        .source_hash()
        .ok()
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct SourceSettings {
    id: String,
    kind: String,
    provider: String,
    #[serde(default = "enabled_default", deserialize_with = "parse_enabled")]
    enabled: bool,
    request_timeout_seconds: Option<f64>,
    #[serde(default)]
    config: Map<String, Value>,
    #[serde(default)]
    secret_refs: BTreeMap<String, String>,
}

fn enabled_default() -> bool {
    true
}

fn parse_enabled<'input, D>(decoder: D) -> Result<bool, D::Error>
where
    D: Deserializer<'input>,
{
    let value = Value::deserialize(decoder)?;
    legacy_boolean(&value).ok_or_else(|| serde::de::Error::custom("invalid enabled flag"))
}

impl SourceSettings {
    fn selected(input: Option<&str>, id: &str) -> Result<Option<Self>, ExtensionError> {
        let Some(input) = input else {
            return Ok(None);
        };
        if input.len() > MAX_SETTINGS_BYTES {
            return Err(ExtensionError::Configuration);
        }
        let integrations: Vec<Value> =
            serde_json::from_str(input).map_err(|_| ExtensionError::Configuration)?;
        let mut matching = integrations.into_iter().filter(|item| item["id"] == id);
        let Some(selected) = matching.next() else {
            return Ok(None);
        };
        if matching.next().is_some() {
            return Err(ExtensionError::Configuration);
        }
        if selected["kind"] != "source" {
            return Ok(None);
        }
        let source: Self =
            serde_json::from_value(selected).map_err(|_| ExtensionError::Configuration)?;
        let mut provider = source.provider.bytes();
        if source.request_timeout_seconds.is_some()
            || source.provider.len() > 100
            || !provider
                .next()
                .is_some_and(|byte| byte.is_ascii_lowercase())
            || !provider.all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(&byte)
            })
            || source.config.len() > 32
            || source
                .config
                .values()
                .any(|value| value.is_null() || value.is_array() || value.is_object())
            || source.secret_refs.len() > 16
            || !source.valid_secret_references()
        {
            return Err(ExtensionError::Configuration);
        }
        Ok(source.enabled.then_some(source))
    }

    fn source_hash(&self) -> Result<String, ExtensionError> {
        let wire = serde_json::to_vec(self).map_err(|_| ExtensionError::Configuration)?;
        let normalized = encode(&wire, Utf8Layout::Compact, MAX_SETTINGS_BYTES)
            .map_err(|_| ExtensionError::Configuration)?;
        Ok(sha256_hex(&normalized))
    }

    fn context(&self) -> Result<Value, ExtensionError> {
        let mut secrets = Map::new();
        for (name, reference) in &self.secret_refs {
            let value = env::var(reference)
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or(ExtensionError::Configuration)?;
            secrets.insert(name.to_owned(), Value::String(value));
        }
        Ok(json!({"config": self.config, "secrets": secrets}))
    }

    fn valid_secret_references(&self) -> bool {
        let valid = |text: &str, max: usize, allowed: fn(&u8) -> bool| {
            let mut bytes = text.bytes();
            text.len() <= max
                && bytes.next().is_some_and(|first| allowed(&first))
                && bytes.all(|byte| allowed(&byte) || byte.is_ascii_digit() || byte == b'_')
        };
        self.secret_refs.iter().all(|(name, reference)| {
            valid(name, 64, u8::is_ascii_lowercase)
                && reference
                    .strip_prefix("GA_PLUGIN_")
                    .is_some_and(|suffix| valid(suffix, 128, u8::is_ascii_uppercase))
        })
    }
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn poll_source(executable: &Path, id: &str) -> Result<(), Box<dyn Error>> {
    let [configured, url] = configuration::read(["GA_INTEGRATIONS", "GA_DATABASE_URL"])?;
    let Some(source) = SourceSettings::selected(configured.as_deref(), id)? else {
        println!("{}", summary("disabled", 0, 0));
        return Ok(());
    };
    let url = url.ok_or("Source extension storage is unavailable.")?;
    let database =
        Database::connect_lazy(&url).map_err(|_| "Source extension storage is unavailable.")?;
    let now = clock()?;
    let (cancel, mut cancelled) = watch::channel(false);
    let report = run_until_shutdown(
        cancel,
        DEADLINE,
        poll(&database, executable, source, now, &mut cancelled),
    )
    .await?;
    println!("{report}");
    Ok(())
}

pub(crate) struct ScheduledSourcePlugins {
    sources: BTreeMap<String, (PathBuf, SourceSettings)>,
}

impl ScheduledSourcePlugins {
    pub(crate) fn read() -> Result<Option<Self>, Box<dyn Error>> {
        let [configured, executables] =
            configuration::read(["GA_INTEGRATIONS", "GA_SOURCE_PLUGIN_EXECUTABLES"])?;
        let Some(executables) = executables else {
            return Ok(None);
        };
        if executables.len() > MAX_SETTINGS_BYTES {
            return Err(ExtensionError::Configuration.into());
        }
        let executables: BTreeMap<String, PathBuf> =
            serde_json::from_str(&executables).map_err(|_| ExtensionError::Configuration)?;
        if executables.len() > 32 {
            return Err(ExtensionError::Configuration.into());
        }
        let mut sources = BTreeMap::new();
        for (id, executable) in executables {
            let Some(source) = SourceSettings::selected(configured.as_deref(), &id)? else {
                continue;
            };
            if source.provider == "garmin" {
                continue;
            }
            Extension::source(&executable, &id)?;
            sources.insert(id, (executable, source));
        }
        Ok((!sources.is_empty()).then_some(Self { sources }))
    }

    pub(crate) async fn run(&self, database: &Database) -> ! {
        let sources: Vec<_> = self.sources.keys().map(String::as_str).collect();
        let mut next_plan = Instant::now();
        loop {
            let Ok(now) = clock() else {
                sleep(PLAN_INTERVAL).await;
                continue;
            };
            if Instant::now() >= next_plan {
                next_plan = Instant::now() + PLAN_INTERVAL;
                if let Err(error) = database.schedule_source_plugin_polls(&sources, now).await {
                    eprintln!("{error}");
                }
            }
            match database.claim_source_plugin_poll(&sources, now).await {
                Ok(Some(lease)) => {
                    let (executable, source) = &self.sources[lease.instance()];
                    let (_stop, mut cancelled) = watch::channel(false);
                    let result = tokio::time::timeout(
                        DEADLINE,
                        poll(database, executable, source.clone(), now, &mut cancelled),
                    )
                    .await;
                    let outcome = match result {
                        Ok(Ok(_)) => JobOutcome::Completed,
                        Ok(Err(error)) => {
                            eprintln!("{error}");
                            JobOutcome::Failed {
                                reason: FailureReason::RuntimeError,
                                retry_at: None,
                            }
                        }
                        Err(_) => {
                            eprintln!("{}", ExtensionError::Timeout);
                            JobOutcome::Failed {
                                reason: FailureReason::RuntimeError,
                                retry_at: None,
                            }
                        }
                    };
                    if let Ok(finished) = clock()
                        && let Err(error) = database
                            .finish_source_plugin_poll(&lease, finished, outcome)
                            .await
                    {
                        eprintln!("{error}");
                    }
                }
                Ok(None) => {}
                Err(error) => eprintln!("{error}"),
            }
            sleep(JOB_INTERVAL).await;
        }
    }
}

fn clock() -> Result<StoredInstant, Box<dyn Error>> {
    let now = OffsetDateTime::now_utc()
        .unix_timestamp_nanos()
        .div_euclid(1000);
    Ok(StoredInstant::from_unix_microseconds(i64::try_from(now)?)?)
}

async fn poll(
    database: &Database,
    executable: &Path,
    source: SourceSettings,
    now: StoredInstant,
    cancelled: &mut watch::Receiver<bool>,
) -> Result<Value, Box<dyn Error>> {
    let owner = database
        .enrolled_owner_id()
        .await
        .map_err(|_| "Source extension owner is unavailable.")?;
    let mut saved = database.prepare_source_poll(owner, &source.id, now).await?;
    match &saved {
        SourcePoll::Disabled => return Ok(summary("disabled", 0, 0)),
        SourcePoll::Deferred(_) => return Ok(summary("deferred", 0, 0)),
        SourcePoll::Ready(_) => {}
    }
    let mut extension = Extension::source(executable, &source.id)?;
    extension.context = Some(source.context()?);
    let hash = source.source_hash()?;
    let capabilities = extension.describe(cancelled).await?;
    let limit = capabilities.max_page_size.min(100);
    let mut records = 0;
    let mut pages = 0;
    while pages < MAX_PAGES {
        let poll = match saved {
            SourcePoll::Ready(poll) => poll,
            SourcePoll::Disabled => return Ok(summary("disabled", pages, records)),
            SourcePoll::Deferred(_) => return Ok(summary("deferred", pages, records)),
        };
        let window = poll.window();
        let page = extension
            .read_page((pages + 2) as u64, window, poll.cursor(), limit, cancelled)
            .await?;
        let published = database
            .publish_source_page_unless_cancelled(*poll, &hash, capabilities, &page, || {
                *cancelled.borrow()
            })
            .await?;
        let status = match published.state {
            PublicationState::Disabled => return Ok(summary("disabled", pages, records)),
            PublicationState::Stale => return Ok(summary("stale", pages, records)),
            PublicationState::Complete => "complete",
            PublicationState::Deferred => "deferred",
            PublicationState::Partial => "partial",
        };
        pages += 1;
        records += published.records;
        if status != "partial" {
            return Ok(summary(status, pages, records));
        }
        saved = database.prepare_source_poll(owner, &source.id, now).await?;
        if let SourcePoll::Ready(next) = &saved
            && (next.cursor() != page.next_cursor.as_deref() || next.window() != window)
        {
            return Ok(summary("stale", pages, records));
        }
    }
    Ok(summary("partial", pages, records))
}

fn summary(status: &str, pages: usize, records: usize) -> Value {
    json!({"status":status,"pages":pages,"records":records})
}
