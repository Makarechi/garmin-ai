use ga_domain::analysis_spec::AnalysisSpec;

use super::GenericAnalysisError;

pub(super) struct SourceSelection {
    pub(super) mode: &'static str,
    pub(super) account: Option<String>,
    pub(super) device: Option<String>,
    pub(super) measurement: Option<String>,
}

impl SourceSelection {
    pub(super) fn from_spec(spec: &AnalysisSpec) -> Result<Self, GenericAnalysisError> {
        Self::from_source(spec.document()["source"].as_str())
    }

    pub(super) fn from_source(source: Option<&str>) -> Result<Self, GenericAnalysisError> {
        let mut selection = Self {
            mode: "all",
            account: None,
            device: None,
            measurement: None,
        };
        if let Some(source) = source {
            if let Some(value) = source.strip_prefix("measurement:") {
                selection.mode = "measurement";
                selection.measurement = Some(value.to_owned());
            } else if let Some(value) = source.strip_prefix("observation:") {
                selection.mode = "observation";
                let [account, device]: [Option<String>; 2] =
                    serde_json::from_str(value).map_err(|_| GenericAnalysisError::Encoding)?;
                selection.account = account;
                selection.device = device;
            } else {
                selection.mode = "event";
            }
        }
        Ok(selection)
    }
}
