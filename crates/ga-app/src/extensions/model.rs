use std::{error::Error, path::Path};

use ga_app::model_gateway::extension_structured_capability;
use ga_domain::{
    model_request::{ApprovedModelSchema, ExtensionModelRequest},
    model_response::MAX_RESPONSE_BYTES,
};
use serde_json::{Value, json};
use tokio::sync::watch;

use super::{Extension, ExtensionError, PROBE_TIMEOUT, run_until_shutdown};

const INSTRUCTION: &str = include_str!("../../../../src/garmin_ai/prompts/extension_probe.txt");

impl Extension<'_> {
    async fn model_probe(
        &self,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<Value, ExtensionError> {
        let valid = self
            .instance_id
            .strip_prefix("model:")
            .and_then(|instance| instance.split_once(':'))
            .is_some_and(|(provider, instance)| !provider.is_empty() && !instance.is_empty());
        if !valid {
            return Err(ExtensionError::ModelConfiguration);
        }
        let capabilities = self
            .describe_kind("model", cancelled, ExtensionError::ModelCapabilities)
            .await?;
        if !extension_structured_capability(capabilities) {
            return Err(ExtensionError::ModelCapabilities);
        }
        let request = ExtensionModelRequest::prepare(
            ApprovedModelSchema::ModelProbe,
            None,
            INSTRUCTION.trim_end(),
            "Synthetic probe",
        )
        .map_err(|_| ExtensionError::ModelResult)?;
        let response = self
            .request(
                2,
                "structured",
                request.params().clone(),
                MAX_RESPONSE_BYTES,
                cancelled,
            )
            .await?;
        request
            .decode_response(response.get().as_bytes())
            .map_err(|_| ExtensionError::ModelResult)?;
        Ok(json!({"structured_output":true}))
    }
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn check_model(
    executable: &Path,
    instance_id: &str,
) -> Result<(), Box<dyn Error>> {
    let extension =
        Extension::new(executable, instance_id).map_err(|_| ExtensionError::ModelConfiguration)?;
    let (cancel, mut cancelled) = watch::channel(false);
    let report = run_until_shutdown(cancel, PROBE_TIMEOUT, async {
        Ok(extension.model_probe(&mut cancelled).await?)
    })
    .await?;
    println!("{report}");
    Ok(())
}
