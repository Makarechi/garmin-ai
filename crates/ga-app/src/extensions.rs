pub(crate) use channel::delivery::ScheduledChannelPlugins;

mod channel;
mod model;
mod source_poll;

pub(crate) use channel::check_channel;
pub(crate) use model::check_model;
pub(crate) use source_poll::{ScheduledSourcePlugins, configured_source_fingerprint, poll_source};

use std::{collections::BTreeSet, error::Error, fmt, future::Future, path::Path, time::Duration};

use ga_app::extension_process::{ExtensionProcess, ExtensionProcessError};
use ga_domain::{
    source_contract::{SourceCapabilities, SourcePage, SourceRequest},
    timestamp::StoredInstant,
};
use serde_json::{Value, json, value::RawValue};
use tokio::{sync::watch, time::sleep};

const MAX_REPLY: usize = 4 * 1024 * 1024;
const MAX_SOURCE_REPLY: usize = 8 * 1024 * 1024;
const MAX_PAGES: usize = 1000;
const CALL_TIMEOUT: Duration = Duration::from_secs(3);
const PROBE_TIMEOUT: Duration = Duration::from_secs(15);

fn valid_secret_reference(slot: &str, reference: &str) -> bool {
    fn valid(value: &str, max: usize, case: fn(&u8) -> bool) -> bool {
        let mut bytes = value.bytes();
        value.len() <= max
            && bytes.next().is_some_and(|byte| case(&byte))
            && bytes.all(|byte| case(&byte) || byte.is_ascii_digit() || byte == b'_')
    }
    valid(slot, 64, u8::is_ascii_lowercase)
        && reference
            .strip_prefix("GA_PLUGIN_")
            .is_some_and(|name| valid(name, 128, u8::is_ascii_uppercase))
}

#[derive(Clone, Copy, Debug)]
enum ExtensionError {
    Configuration,
    Timeout,
    Process(ExtensionProcessError),
    Cancelled,
    Capabilities,
    SourcePage,
    ChannelConfiguration,
    ChannelCapabilities,
    ChannelDelivery,
    ModelConfiguration,
    ModelCapabilities,
    ModelResult,
}

impl fmt::Display for ExtensionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Process(error) => return error.fmt(f),
            Self::Configuration => "The source extension configuration is invalid.",
            Self::Timeout => "The extension exceeded its response deadline.",
            Self::Cancelled => "The extension check was cancelled.",
            Self::Capabilities => "The extension's source capabilities are invalid.",
            Self::SourcePage => "The extension returned a page outside the source contract.",
            Self::ChannelConfiguration => "The channel extension configuration is invalid.",
            Self::ChannelCapabilities => "The extension's channel capabilities are invalid.",
            Self::ChannelDelivery => {
                "The extension returned a delivery outside the channel contract."
            }
            Self::ModelConfiguration => "The model extension configuration is invalid.",
            Self::ModelCapabilities => "The extension's model capabilities are invalid.",
            Self::ModelResult => "The extension returned a result outside the model contract.",
        })
    }
}

impl Error for ExtensionError {}

struct Extension<'a> {
    instance_id: &'a str,
    process: ExtensionProcess<'a>,
}

impl<'a> Extension<'a> {
    fn new(executable: &'a Path, instance_id: &'a str) -> Result<Self, ExtensionError> {
        Ok(Self {
            instance_id,
            process: ExtensionProcess::new(executable, instance_id)
                .map_err(|_| ExtensionError::Configuration)?,
        })
    }

    async fn request(
        &self,
        request_id: u64,
        method: &str,
        params: Value,
        max_reply: usize,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<Box<RawValue>, ExtensionError> {
        self.process
            .request(
                request_id,
                method,
                params,
                max_reply,
                CALL_TIMEOUT,
                cancelled,
            )
            .await
            .map_err(ExtensionError::Process)
    }

    async fn describe(
        &self,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<SourceCapabilities, ExtensionError> {
        let capabilities = self
            .describe_kind("source", cancelled, ExtensionError::Capabilities)
            .await?;
        SourceCapabilities::from_json(&capabilities)
            .ok()
            .filter(|capabilities| capabilities.observations)
            .ok_or(ExtensionError::Capabilities)
    }

    async fn describe_kind(
        &self,
        kind: &str,
        cancelled: &mut watch::Receiver<bool>,
        invalid: ExtensionError,
    ) -> Result<Value, ExtensionError> {
        self.process
            .describe(kind, MAX_REPLY, CALL_TIMEOUT, cancelled)
            .await
            .map_err(|error| match error {
                ExtensionProcessError::Descriptor => invalid,
                _ => ExtensionError::Process(error),
            })
    }

    async fn read_page(
        &self,
        request_id: u64,
        window: (StoredInstant, StoredInstant),
        cursor: Option<&str>,
        limit: usize,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<SourcePage, ExtensionError> {
        let (start, end) = window;
        let raw = self.request(
            request_id,
            "read_page",
            json!({"start": start.to_string(), "end": end.to_string(), "cursor": cursor, "limit": limit}),
            MAX_SOURCE_REPLY,
            cancelled,
        ).await?;
        SourcePage::from_json(raw.get().as_bytes(), limit).map_err(|_| ExtensionError::SourcePage)
    }

    async fn source_probe(
        &self,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<Value, ExtensionError> {
        let capabilities = self.describe(cancelled).await?;
        let start: StoredInstant = "2026-01-01T00:00:00Z"
            .parse()
            .map_err(|_| ExtensionError::Configuration)?;
        let end: StoredInstant = "2026-01-02T00:00:00Z"
            .parse()
            .map_err(|_| ExtensionError::Configuration)?;
        let limit = capabilities.max_page_size.min(2);
        let mut cursor = None;
        let mut previous_cursors = BTreeSet::new();
        let mut records = BTreeSet::new();
        for pages in 1..=MAX_PAGES {
            let page = self
                .read_page(
                    (pages + 1) as u64,
                    (start, end),
                    cursor.as_deref(),
                    limit,
                    cancelled,
                )
                .await?;
            let next_digest = page
                .check_request(&SourceRequest {
                    instance_id: self.instance_id,
                    start,
                    end,
                    cursor: cursor.as_deref(),
                    previous_cursors: &previous_cursors,
                    capabilities,
                })
                .map_err(|_| ExtensionError::SourcePage)?;
            records.extend(
                page.records
                    .into_iter()
                    .map(|record| record.source_record_id),
            );
            if let Some(digest) = next_digest {
                previous_cursors.insert(digest);
                cursor = page.next_cursor;
            } else if records.is_empty() {
                return Err(ExtensionError::SourcePage);
            } else {
                return Ok(
                    json!({"instance_id": self.instance_id, "pages": pages, "records": records.len()}),
                );
            }
        }
        Err(ExtensionError::SourcePage)
    }
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn check_source(
    executable: &Path,
    instance_id: &str,
) -> Result<(), Box<dyn Error>> {
    let extension = Extension::new(executable, instance_id)?;
    let (cancel, mut cancelled) = watch::channel(false);
    let report = run_until_shutdown(cancel, PROBE_TIMEOUT, async {
        Ok(extension.source_probe(&mut cancelled).await?)
    })
    .await?;
    println!("{report}");
    Ok(())
}

async fn run_until_shutdown<T>(
    cancel: watch::Sender<bool>,
    deadline: Duration,
    task: impl Future<Output = Result<T, Box<dyn Error>>>,
) -> Result<T, Box<dyn Error>> {
    #[cfg(unix)]
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let interrupted = async {
        #[cfg(unix)]
        tokio::select! {
            _ = tokio::signal::ctrl_c() => (),
            _ = terminate.recv() => (),
        }
        #[cfg(not(unix))]
        let _ = tokio::signal::ctrl_c().await;
    };
    tokio::pin!(task);
    let reason = tokio::select! {
        result = &mut task => return result,
        () = interrupted => ExtensionError::Cancelled,
        () = sleep(deadline) => ExtensionError::Timeout,
    };
    let _ = cancel.send(true);
    let _ = task.await;
    Err(reason.into())
}
