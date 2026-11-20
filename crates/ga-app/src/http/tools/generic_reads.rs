use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use ga_db::health_analysis::{HealthAnalysisError, generic::GenericAnalysisError};
use ga_domain::analysis_spec::{AnalysisSpec, InvalidAnalysisSpec};
use serde_json::{Map, Value, json};

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

pub(super) fn validation_failure(
    arguments: &Map<String, Value>,
    error: InvalidAnalysisSpec,
) -> Response {
    if matches!(error, InvalidAnalysisSpec::CalendarOverflow) {
        return internal_error();
    }
    let mut errors = if let Some(spec) = arguments.get("spec") {
        let errors = AnalysisSpec::field_errors(spec);
        if errors.is_empty() && (arguments.len() == 1 || AnalysisSpec::from_json(spec).is_err()) {
            // Python includes a model-level ValueError in the exception context.
            // Its HTTP error renderer cannot serialize that value.
            return internal_error();
        }
        errors
    } else {
        vec![json!({
            "type": "missing", "loc": ["spec"], "msg": "Field required", "input": arguments,
        })]
    };
    errors.extend(
        arguments
            .iter()
            .filter(|(name, _)| *name != "spec")
            .map(|(name, value)| {
                json!({
                    "type": "extra_forbidden", "loc": [name],
                    "msg": "Extra inputs are not permitted", "input": value,
                })
            }),
    );
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(json!({"detail": errors})),
    )
        .into_response()
}

fn internal_error() -> Response {
    (StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error").into_response()
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
