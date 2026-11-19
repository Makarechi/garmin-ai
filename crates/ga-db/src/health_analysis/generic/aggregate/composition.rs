use std::time::Duration;

use ga_archive::json_spool::JsonDocument;
use ga_domain::{
    analysis_spec::AnalysisSpec,
    legacy_json::{multiply_numbers, sum_numbers},
};
use serde_json::{Value, json};

use crate::health_analysis::generic::{GenericAnalysisError, GenericAnalysisReader};

fn numeric(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(value) => Some(f64::from(u8::from(*value))),
        value => value.as_f64(),
    }
}

fn difference(first: &Value, second: &Value) -> Result<Value, GenericAnalysisError> {
    let (Some(first_number), Some(second_number)) = (numeric(first), numeric(second)) else {
        return Ok(Value::Null);
    };
    if (first.is_boolean() || first.as_i64().is_some())
        && (second.is_boolean() || second.as_i64().is_some())
    {
        let negative = multiply_numbers(first, &Value::from(-1)).map_err(|_| {
            GenericAnalysisError::Calculation(
                ga_domain::metric_aggregate::MetricAggregateError::Nonfinite,
            )
        })?;
        return sum_numbers([second, &negative]).map_err(|_| {
            GenericAnalysisError::Calculation(
                ga_domain::metric_aggregate::MetricAggregateError::Nonfinite,
            )
        });
    }
    serde_json::Number::from_f64(second_number - first_number)
        .map(Value::Number)
        .ok_or(GenericAnalysisError::Calculation(
            ga_domain::metric_aggregate::MetricAggregateError::Nonfinite,
        ))
}

impl GenericAnalysisReader {
    pub async fn compare_periods(
        mut self,
        spec: &AnalysisSpec,
    ) -> Result<JsonDocument, GenericAnalysisError> {
        let work = async {
            self.prepare(spec).await?;
            let first_spec = spec
                .derive_aggregate()
                .map_err(|_| GenericAnalysisError::Database)?;
            let first = self.aggregate_payload(&first_spec).await?;
            let second_spec = spec
                .derive_comparison_aggregate()
                .map_err(|_| GenericAnalysisError::Database)?;
            let second = self.aggregate_payload(&second_spec).await?;
            let comparable = [
                "metric_version",
                "unit",
                "scale_id",
                "scale_version",
                "method",
                "source",
            ]
            .iter()
            .all(|key| first[key] == second[key]);
            let difference = if comparable {
                difference(&first["value"], &second["value"])?
            } else {
                Value::Null
            };
            let mut payload = json!({"spec_hash":spec.hash(),"comparable":comparable,"first":null,"second":null,"difference":difference});
            payload["first"] = first;
            payload["second"] = second;
            self.finish_aggregate_document(payload).await
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| GenericAnalysisError::Database)?
    }

    pub async fn query_completeness(
        mut self,
        spec: &AnalysisSpec,
    ) -> Result<JsonDocument, GenericAnalysisError> {
        let work = async {
            self.prepare(spec).await?;
            let derived = spec
                .derive_aggregate()
                .map_err(|_| GenericAnalysisError::Database)?;
            let aggregate = self.aggregate_payload(&derived).await?;
            let payload = json!({
                "spec_hash":spec.hash(),"metric":spec.document()["metric_key"],
                "observations":aggregate["observations"],"coverage_ratio":aggregate["coverage_ratio"],
                "aggregate_available":!aggregate["value"].is_null(),"reporting_completeness":"unknown",
                "complete":null,"knowledge_cutoff":aggregate["knowledge_cutoff"],
            });
            drop(aggregate);
            self.finish_aggregate_document(payload).await
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| GenericAnalysisError::Database)?
    }
}
