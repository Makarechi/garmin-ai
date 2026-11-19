use ga_archive::json_spool::JsonDocument;
use ga_db::health_analysis::{
    HealthAnalysisError,
    generic::{GenericAnalysisError, GenericAnalysisReader},
};
use ga_domain::analysis_spec::{AnalysisOperation, AnalysisSpec, InvalidAnalysisSpec};
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

pub(crate) async fn read_generic(
    reader: GenericAnalysisReader,
    spec: &AnalysisSpec,
) -> Result<JsonDocument, GenericAnalysisError> {
    match spec.operation() {
        AnalysisOperation::QueryEntries => reader.query_entries(spec).await,
        AnalysisOperation::QueryObservations => reader.query_observations(spec).await,
        AnalysisOperation::AggregateMetric => reader.aggregate_metric(spec).await,
        AnalysisOperation::ComparePeriods => reader.compare_periods(spec).await,
        AnalysisOperation::QueryCompleteness => reader.query_completeness(spec).await,
        AnalysisOperation::ThresholdSegments => reader.threshold_segments(spec).await,
        AnalysisOperation::ThresholdByLocalHour => reader.threshold_by_local_hour(spec).await,
    }
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
