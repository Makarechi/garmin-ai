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
#[must_use]
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
        ExtensionProcess::for_model(executable, instance_id).map_err(map_error)?;
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
            ExtensionProcess::for_model(&self.executable, &self.instance_id).map_err(map_error)?;
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
        | ExtensionProcessError::Cancelled
        | ExtensionProcessError::Unavailable => unavailable(),
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

#[cfg(all(test, unix))]
mod tests {
    use std::{error::Error, fs, os::unix::fs::PermissionsExt};

    use ga_domain::model_request::ApprovedModelSchema;
    use serde_json::json;

    use super::*;

    #[tokio::test]
    async fn sends_application_sized_model_requests_and_rejects_oversized_envelopes()
    -> Result<(), Box<dyn Error>> {
        let directory = tempfile::tempdir()?;
        let executable = directory.path().join("model");
        fs::write(
            &executable,
            r#"#!/bin/sh
IFS= read -r request
case "$request" in
    *'"method":"describe"'*)
        printf '%s\n' '{"version":1,"request_id":1,"result":{"kind":"model","instance_id":"model:synthetic:size","implementation_version":"v1","capabilities":{"structured_output":true}}}'
        ;;
    *'"method":"structured"'*)
        [ "${#request}" -gt 96000 ] || exit 1
        printf '%s\n' '{"version":1,"request_id":2,"result":{"value":{"urgent":false}}}'
        ;;
esac
"#,
        )?;
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700))?;
        let transport = ExtensionModelTransport::new(
            &executable,
            "model:synthetic:size",
            &json!({"config":{"fixture":"synthetic"},"secrets":{}}),
        )?;
        let request = |prompt: &str| {
            ExtensionModelRequest::prepare(
                ApprovedModelSchema::ModelProbe,
                Some("synthetic-size"),
                "Synthetic instruction",
                prompt,
            )
        };
        let entry = r#"{"text":"synthetic"}"#;
        let prompt = format!("[{}]", vec![entry; 4_500].join(","));
        assert!(prompt.len() < 96_000);
        let output = transport
            .structured(&request(&prompt)?, Duration::from_secs(2))
            .await?;
        assert_eq!(output.value, json!({"urgent":false}));
        let oversized = request(&"x".repeat(1024 * 1024))?;
        assert!(matches!(
            transport
                .structured(&oversized, Duration::from_secs(2))
                .await,
            Err(ModelTransportError::Response(
                ModelResponseError::InvalidEnvelope
            ))
        ));
        Ok(())
    }
}
