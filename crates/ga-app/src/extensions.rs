mod channel;
mod model;
mod source_poll;

pub(crate) use channel::check_channel;
pub(crate) use model::check_model;
pub(crate) use source_poll::{ScheduledSourcePlugins, configured_source_fingerprint, poll_source};

use std::{
    collections::BTreeSet, error::Error, fmt, future::Future, path::Path, process::Stdio,
    time::Duration,
};

use ga_domain::{
    source_contract::{SourceCapabilities, SourcePage, SourceRequest},
    timestamp::StoredInstant,
};
use serde::{Deserialize, Deserializer};
use serde_json::{Value, json, value::RawValue};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::watch,
    time::{sleep, timeout},
};

const VERSION: u8 = 1;
const MAX_REQUEST: usize = 64 * 1024;
const MAX_REPLY: usize = 4 * 1024 * 1024;
const MAX_SOURCE_REPLY: usize = 8 * 1024 * 1024;
const MAX_PAGES: usize = 1000;
const CALL_TIMEOUT: Duration = Duration::from_secs(3);
const PROBE_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Clone, Copy, Debug)]
enum ExtensionError {
    Configuration,
    Launch,
    Timeout,
    Cancelled,
    Transport,
    Protocol,
    Rejected,
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
            Self::Configuration => "The source extension configuration is invalid.",
            Self::Launch => "The configured extension could not be started.",
            Self::Timeout => "The extension exceeded its response deadline.",
            Self::Cancelled => "The extension check was cancelled.",
            Self::Transport => "The extension process failed.",
            Self::Protocol => "The extension returned an invalid response envelope.",
            Self::Rejected => "The extension refused the request.",
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

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Reply<'a> {
    version: u8,
    request_id: u64,
    #[serde(borrow, default, deserialize_with = "present")]
    result: Option<&'a RawValue>,
    #[serde(default, deserialize_with = "present")]
    error: Option<WireError>,
}

fn present<'input, D, T>(decoder: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'input>,
    T: Deserialize<'input>,
{
    T::deserialize(decoder).map(Some)
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum WireError {
    Unsupported,
    InvalidRequest,
    Unavailable,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExtensionDescriptor {
    kind: String,
    instance_id: String,
    implementation_version: String,
    capabilities: Value,
}

struct Extension<'a> {
    executable: &'a Path,
    directory: &'a Path,
    instance_id: &'a str,
    context: Option<Value>,
}

impl<'a> Extension<'a> {
    fn new(executable: &'a Path, instance_id: &'a str) -> Result<Self, ExtensionError> {
        let mut id = instance_id.bytes();
        let directory = executable.parent().filter(|_| executable.is_absolute());
        if instance_id.len() > 200
            || !id.next().is_some_and(|byte| byte.is_ascii_lowercase())
            || !id.all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.:-".contains(&byte)
            })
        {
            return Err(ExtensionError::Configuration);
        }
        Ok(Self {
            executable,
            directory: directory.ok_or(ExtensionError::Configuration)?,
            instance_id,
            context: None,
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
        let mut envelope = json!({
            "version": VERSION,
            "request_id": request_id,
            "instance_id": self.instance_id,
            "method": method,
            "params": params,
        });
        if let Some(context) = &self.context {
            envelope["context"] = context.clone();
        }
        let mut wire = serde_json::to_vec(&envelope).map_err(|_| ExtensionError::Protocol)?;
        if wire.len() >= MAX_REQUEST {
            return Err(ExtensionError::Protocol);
        }
        wire.push(b'\n');
        let response = self.exchange(&wire, max_reply, cancelled).await?;
        let line = response
            .strip_suffix(b"\n")
            .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
            .filter(|line| !line.contains(&b'\n'))
            .ok_or(ExtensionError::Protocol)?;
        let reply: Reply<'_> =
            serde_json::from_slice(line).map_err(|_| ExtensionError::Protocol)?;
        if reply.version != VERSION || reply.request_id != request_id {
            return Err(ExtensionError::Protocol);
        }
        match (reply.result, reply.error) {
            (Some(result), None) => Ok(result.to_owned()),
            (None, Some(_)) => Err(ExtensionError::Rejected),
            _ => Err(ExtensionError::Protocol),
        }
    }

    async fn exchange(
        &self,
        request: &[u8],
        max_reply: usize,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<Vec<u8>, ExtensionError> {
        if *cancelled.borrow() {
            return Err(ExtensionError::Cancelled);
        }
        let mut child = Command::new(self.executable)
            .current_dir(self.directory)
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| ExtensionError::Launch)?;
        let mut stdin = child.stdin.take().ok_or(ExtensionError::Transport)?;
        let stdout = child.stdout.take().ok_or(ExtensionError::Transport)?;
        let mut response = Vec::new();
        let mut bounded_output = stdout.take((max_reply + 1) as u64);
        let exchange = async {
            tokio::try_join!(
                async move {
                    stdin.write_all(request).await?;
                    drop(stdin);
                    Ok(())
                },
                bounded_output.read_to_end(&mut response)
            )
            .map_err(|_| ExtensionError::Transport)?;
            if response.len() > max_reply {
                return Err(ExtensionError::Protocol);
            }
            let status = child.wait().await.map_err(|_| ExtensionError::Transport)?;
            if status.success() {
                Ok(response)
            } else {
                Err(ExtensionError::Transport)
            }
        };
        let result = tokio::select! {
            _ = cancelled.changed() => Err(ExtensionError::Cancelled),
            result = timeout(CALL_TIMEOUT, exchange) => result.unwrap_or(Err(ExtensionError::Timeout)),
        };
        if result.is_err() {
            // Kill and reap the direct child before returning after a deadline or signal.
            let _ = child.kill().await;
        }
        result
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
        let descriptor = self
            .request(1, "describe", Value::Null, MAX_REPLY, cancelled)
            .await?;
        let descriptor: ExtensionDescriptor =
            serde_json::from_str(descriptor.get()).map_err(|_| invalid)?;
        if descriptor.kind != kind
            || descriptor.instance_id != self.instance_id
            || descriptor.implementation_version.is_empty()
            || descriptor.implementation_version.len() > 100
        {
            return Err(invalid);
        }
        Ok(descriptor.capabilities)
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
