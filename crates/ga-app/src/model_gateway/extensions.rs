use std::{
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use ga_domain::{
    model_request::ExtensionModelRequest,
    model_response::{MAX_RESPONSE_BYTES, ModelResponseError, StructuredModelOutput},
};
use ga_model::{ModelTransportError, ModelUnavailability};
use serde::Deserialize;
use serde_json::Value;
use tokio::sync::watch;

use crate::extension_process::{ExtensionProcess, ExtensionProcessError};

const RETRY_AFTER: Duration = Duration::from_secs(15);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelCapabilities {
    structured_output: bool,
    #[serde(default, rename = "transcription")]
    _transcription: bool,
    #[serde(default, rename = "cancellation")]
    _cancellation: bool,
}

/// Check the structured capability in a versioned model descriptor.
pub fn extension_structured_capability(capabilities: Value) -> bool {
    serde_json::from_value::<ModelCapabilities>(capabilities)
        .is_ok_and(|capabilities| capabilities.structured_output)
}

pub(super) struct ExtensionModelTransport {
    executable: PathBuf,
    instance_id: String,
    context: Value,
}

impl ExtensionModelTransport {
    pub(super) fn new(
        executable: &Path,
        instance_id: &str,
        context: &Value,
    ) -> Result<Self, ModelTransportError> {
        ExtensionProcess::new(executable, instance_id).map_err(map_error)?;
        Ok(Self {
            executable: executable.to_path_buf(),
            instance_id: instance_id.to_owned(),
            context: context.clone(),
        })
    }

    pub(super) async fn structured(
        &self,
        input: &ExtensionModelRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        let mut process =
            ExtensionProcess::new(&self.executable, &self.instance_id).map_err(map_error)?;
        process.set_context(self.context.clone());
        let (_cancel, mut cancelled) = watch::channel(false);
        let start = Instant::now();
        let capabilities = process
            .describe("model", MAX_RESPONSE_BYTES, remaining, &mut cancelled)
            .await
            .map_err(map_error)?;
        if !extension_structured_capability(capabilities) {
            return Err(ModelTransportError::InvalidConfiguration);
        }
        let remaining = remaining
            .checked_sub(start.elapsed())
            .ok_or_else(unavailable)?;
        let result = process
            .request(
                2,
                "structured",
                input.params().clone(),
                MAX_RESPONSE_BYTES,
                remaining,
                &mut cancelled,
            )
            .await
            .map_err(map_error)?;
        input
            .decode_response(result.get().as_bytes())
            .map_err(ModelTransportError::Response)
    }
}

fn map_error(error: ExtensionProcessError) -> ModelTransportError {
    match error {
        ExtensionProcessError::Configuration | ExtensionProcessError::Descriptor => {
            ModelTransportError::InvalidConfiguration
        }
        ExtensionProcessError::Launch
        | ExtensionProcessError::Transport
        | ExtensionProcessError::Timeout
        | ExtensionProcessError::Cancelled => unavailable(),
        ExtensionProcessError::Protocol => {
            ModelTransportError::Response(ModelResponseError::InvalidEnvelope)
        }
        ExtensionProcessError::Rejected => {
            ModelTransportError::Response(ModelResponseError::Refused)
        }
    }
}

fn unavailable() -> ModelTransportError {
    ModelTransportError::Unavailable {
        reason: ModelUnavailability::Upstream,
        retry_after: RETRY_AFTER,
    }
}
