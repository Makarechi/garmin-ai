use std::{error::Error, path::Path};

use ga_domain::model_response::{ExtensionResponseSchema, MAX_RESPONSE_BYTES};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::watch;

use super::{Extension, ExtensionError, PROBE_TIMEOUT, run_until_shutdown};

const INSTRUCTION: &str = include_str!("../../../../src/garmin_ai/prompts/extension_probe.txt");

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Capabilities {
    structured_output: bool,
    #[serde(default, rename = "transcription")]
    _transcription: bool,
    #[serde(default, rename = "cancellation")]
    _cancellation: bool,
}

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
        let capabilities: Capabilities =
            serde_json::from_value(capabilities).map_err(|_| ExtensionError::ModelCapabilities)?;
        if !capabilities.structured_output {
            return Err(ExtensionError::ModelCapabilities);
        }
        let schema = json!({
            "title":"ModelProbe","type":"object","additionalProperties":false,
            "properties":{"urgent":{"title":"Urgent","type":"boolean"}},
            "required":["urgent"],
        });
        let validator =
            ExtensionResponseSchema::new(&schema).map_err(|_| ExtensionError::ModelResult)?;
        let response = self.request(
            2,
            "structured",
            json!({"instruction":INSTRUCTION.trim_end(),"prompt":"Synthetic probe","schema":schema}),
            MAX_RESPONSE_BYTES,
            cancelled,
        ).await?;
        validator
            .decode(response.get().as_bytes())
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
