use ga_db::health_analysis::{HealthAnalysisError, generic::GenericAnalysisError};
use ga_domain::analysis_spec::{AnalysisSpec, InvalidAnalysisSpec};
use serde_json::{Map, Value};

pub(crate) fn generic_input(
    arguments: &Map<String, Value>,
) -> Result<AnalysisSpec, InvalidAnalysisSpec> {
    if arguments.len() != 1 {
        return Err(InvalidAnalysisSpec::Validation);
    }
    AnalysisSpec::from_json(
        arguments
            .get("spec")
            .ok_or(InvalidAnalysisSpec::Validation)?,
    )
}

pub(crate) fn generic_error_class(error: &GenericAnalysisError) -> &'static str {
    match error {
        GenericAnalysisError::Lookup(_) => "LookupError",
        GenericAnalysisError::Value(_) => "ValueError",
        GenericAnalysisError::Data => "DataError",
        GenericAnalysisError::Encoding => "UnicodeEncodeError",
        GenericAnalysisError::InvalidSnapshot => "AttributeError",
        GenericAnalysisError::Calendar => "OverflowError",
        GenericAnalysisError::Calculation(error) => error.class_name(),
        GenericAnalysisError::Permission => "PermissionError",
        GenericAnalysisError::Health(HealthAnalysisError::ReplayPending) => "ReplayUnavailable",
        _ => "RuntimeError",
    }
}
