//! Bounded JSON exchange with an explicitly configured local extension.

use std::{error::Error, fmt, path::Path, process::Stdio, time::Duration};

use serde::{Deserialize, Deserializer};
use serde_json::{Value, json, value::RawValue};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::watch,
    time::timeout,
};

const VERSION: u8 = 1;
const MAX_REQUEST: usize = 64 * 1024;

#[derive(Clone, Copy, Debug)]
pub enum ExtensionProcessError {
    Configuration,
    Launch,
    Timeout,
    Cancelled,
    Transport,
    Protocol,
    Rejected,
    Descriptor,
}

impl fmt::Display for ExtensionProcessError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Configuration => "The extension configuration is invalid.",
            Self::Launch => "The configured extension could not be started.",
            Self::Timeout => "The extension exceeded its response deadline.",
            Self::Cancelled => "The extension check was cancelled.",
            Self::Transport => "The extension process failed.",
            Self::Protocol => "The extension returned an invalid response envelope.",
            Self::Rejected => "The extension refused the request.",
            Self::Descriptor => "The extension returned an invalid descriptor.",
        })
    }
}

impl Error for ExtensionProcessError {}

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
struct Descriptor {
    kind: String,
    instance_id: String,
    implementation_version: String,
    capabilities: Value,
}

pub struct ExtensionProcess<'a> {
    executable: &'a Path,
    directory: &'a Path,
    instance_id: &'a str,
    context: Option<Value>,
}

impl<'a> ExtensionProcess<'a> {
    pub fn new(executable: &'a Path, instance_id: &'a str) -> Result<Self, ExtensionProcessError> {
        let mut id = instance_id.bytes();
        let directory = executable.parent().filter(|_| executable.is_absolute());
        if instance_id.len() > 200
            || !id.next().is_some_and(|byte| byte.is_ascii_lowercase())
            || !id.all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.:-".contains(&byte)
            })
        {
            return Err(ExtensionProcessError::Configuration);
        }
        Ok(Self {
            executable,
            directory: directory.ok_or(ExtensionProcessError::Configuration)?,
            instance_id,
            context: None,
        })
    }

    /// Set the context explicitly supplied to the extension instead of inheriting environment variables.
    pub fn set_context(&mut self, context: Value) {
        self.context = Some(context);
    }

    pub async fn describe(
        &self,
        kind: &str,
        max_reply: usize,
        deadline: Duration,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<Value, ExtensionProcessError> {
        let response = self
            .request(1, "describe", Value::Null, max_reply, deadline, cancelled)
            .await?;
        let descriptor: Descriptor =
            serde_json::from_str(response.get()).map_err(|_| ExtensionProcessError::Descriptor)?;
        if descriptor.kind != kind
            || descriptor.instance_id != self.instance_id
            || descriptor.implementation_version.is_empty()
            || descriptor.implementation_version.len() > 100
        {
            return Err(ExtensionProcessError::Descriptor);
        }
        Ok(descriptor.capabilities)
    }

    pub async fn request(
        &self,
        request_id: u64,
        method: &str,
        params: Value,
        max_reply: usize,
        deadline: Duration,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<Box<RawValue>, ExtensionProcessError> {
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
        let mut wire =
            serde_json::to_vec(&envelope).map_err(|_| ExtensionProcessError::Protocol)?;
        if wire.len() >= MAX_REQUEST {
            return Err(ExtensionProcessError::Protocol);
        }
        wire.push(b'\n');
        let response = self.exchange(&wire, max_reply, deadline, cancelled).await?;
        let line = response
            .strip_suffix(b"\n")
            .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
            .filter(|line| !line.contains(&b'\n'))
            .ok_or(ExtensionProcessError::Protocol)?;
        let reply: Reply<'_> =
            serde_json::from_slice(line).map_err(|_| ExtensionProcessError::Protocol)?;
        if reply.version != VERSION || reply.request_id != request_id {
            return Err(ExtensionProcessError::Protocol);
        }
        match (reply.result, reply.error) {
            (Some(result), None) => Ok(result.to_owned()),
            (None, Some(_)) => Err(ExtensionProcessError::Rejected),
            _ => Err(ExtensionProcessError::Protocol),
        }
    }

    async fn exchange(
        &self,
        request: &[u8],
        max_reply: usize,
        deadline: Duration,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<Vec<u8>, ExtensionProcessError> {
        if *cancelled.borrow() {
            return Err(ExtensionProcessError::Cancelled);
        }
        if deadline.is_zero() {
            return Err(ExtensionProcessError::Timeout);
        }
        let read_limit = max_reply
            .checked_add(1)
            .ok_or(ExtensionProcessError::Protocol)? as u64;
        let mut child = Command::new(self.executable)
            .current_dir(self.directory)
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| ExtensionProcessError::Launch)?;
        let mut stdin = child.stdin.take().ok_or(ExtensionProcessError::Transport)?;
        let stdout = child
            .stdout
            .take()
            .ok_or(ExtensionProcessError::Transport)?;
        let mut response = Vec::new();
        let mut bounded_output = stdout.take(read_limit);
        let exchange = async {
            tokio::try_join!(
                async move {
                    stdin.write_all(request).await?;
                    drop(stdin);
                    Ok(())
                },
                bounded_output.read_to_end(&mut response)
            )
            .map_err(|_| ExtensionProcessError::Transport)?;
            if response.len() > max_reply {
                return Err(ExtensionProcessError::Protocol);
            }
            let status = child
                .wait()
                .await
                .map_err(|_| ExtensionProcessError::Transport)?;
            if status.success() {
                Ok(response)
            } else {
                Err(ExtensionProcessError::Transport)
            }
        };
        let result = tokio::select! {
            _ = cancelled.changed() => Err(ExtensionProcessError::Cancelled),
            result = timeout(deadline, exchange) => result.unwrap_or(Err(ExtensionProcessError::Timeout)),
        };
        if result.is_err() {
            // Kill and reap the direct child before returning after a deadline or signal.
            let _ = child.kill().await;
        }
        result
    }
}
