use std::time::Duration;

use ga_domain::{
    legacy_json::{Utf8Layout, encode_ascii},
    model_consent::ModelCategories,
    sha256_hex,
    statistics::describe,
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, format_description::well_known::Iso8601};

use super::{
    HealthAnalysisError, HealthPeriod, begin_daily_analysis, daily_metric, model_metric_packs,
    observed_values, period_comparison, period_document, read_daily::model_packs_allowed,
    require_current_projection,
};
use crate::{
    Database,
    conversation_memory::{AnalysisSubject, analysis_channel_in},
    model_gate::onboarding::onboarding_allows_model,
};

pub const PERSONAL_BASELINE: &str = "personal_baseline";
pub const PERIOD_COMPARISON: &str = "analysis_compare_periods";

#[derive(Deserialize, Serialize)]
#[serde(tag = "tool", content = "arguments", deny_unknown_fields)]
pub enum DailyAnalysisQuery {
    #[serde(rename = "personal_baseline")]
    Baseline {
        metric: String,
        start: String,
        end: String,
    },
    #[serde(rename = "analysis_compare_periods")]
    Comparison {
        metric: String,
        a_start: String,
        a_end: String,
        b_start: String,
        b_end: String,
    },
}

impl DailyAnalysisQuery {
    pub fn from_arguments(tool: &str, arguments: &Map<String, Value>) -> Option<Self> {
        serde_json::from_value(json!({"tool":tool,"arguments":arguments})).ok()
    }

    pub(crate) async fn read_in(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        context: AnalysisSubject<'_>,
    ) -> Result<Option<Value>, HealthAnalysisError> {
        let metric = match self {
            Self::Baseline { metric, .. } | Self::Comparison { metric, .. } => metric,
        };
        let (metric, sql) = daily_metric(metric)?;
        require_current_projection(txn).await?;
        if !health_allowed(txn, context).await?
            || !model_packs_allowed(txn, context.binding().owner_id, model_metric_packs(metric))
                .await?
        {
            return Ok(None);
        }
        let period = |start: &str, end: &str| HealthPeriod::new(parse_day(start)?, parse_day(end)?);
        let result = match self {
            Self::Baseline { start, end, .. } => {
                let period = period(start, end)?;
                let values = observed_values(txn, sql, period).await?;
                period_document(
                    metric,
                    period,
                    &values,
                    describe(values.iter().map(|(_, value)| Some(*value))),
                )
            }
            Self::Comparison {
                a_start,
                a_end,
                b_start,
                b_end,
                ..
            } => period_comparison(
                txn,
                metric,
                period(a_start, a_end)?,
                period(b_start, b_end)?,
            )
            .await?
            .document(),
        };
        Ok(Some(result))
    }
}

#[derive(Deserialize, Serialize)]
pub struct DailyAnalysisEvidence {
    query: DailyAnalysisQuery,
    result_hash: String,
}

impl DailyAnalysisEvidence {
    pub fn is_daily_tool(tool: &str) -> bool {
        matches!(tool, PERSONAL_BASELINE | PERIOD_COMPARISON)
    }

    pub fn from_read(evidence: &Value) -> Option<Self> {
        let arguments = evidence["arguments"].as_object()?;
        let query = DailyAnalysisQuery::from_arguments(evidence["tool"].as_str()?, arguments)?;
        let result = &evidence["result"];
        if !result.is_object() || result.get("error").is_some() {
            return None;
        }
        Some(Self {
            query,
            result_hash: result_hash(result)?,
        })
    }

    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        context: AnalysisSubject<'_>,
    ) -> Result<bool, HealthAnalysisError> {
        let value = self.query.read_in(txn, context).await?;
        Ok(value.as_ref().and_then(result_hash).as_deref() == Some(&self.result_hash))
    }

    pub(crate) fn specification(&self) -> Value {
        let mut spec = serde_json::to_value(&self.query).unwrap_or(Value::Null);
        spec["result_hash"] = json!(self.result_hash);
        spec
    }
}

fn result_hash(value: &Value) -> Option<String> {
    let bytes = serde_json::to_vec(value).ok()?;
    Some(sha256_hex(
        encode_ascii(&bytes, Utf8Layout::Archive, bytes.len())
            .ok()?
            .as_bytes(),
    ))
}

impl Database {
    pub async fn analysis_health_allowed(&self, context: AnalysisSubject<'_>) -> bool {
        timeout_read(async {
            let mut txn = begin_daily_analysis(&self.work).await?;
            let allowed = health_allowed(&mut txn, context).await?;
            txn.commit().await?;
            Ok(allowed)
        })
        .await
        .unwrap_or(false)
    }

    pub async fn read_analysis_daily(
        &self,
        context: AnalysisSubject<'_>,
        query: &DailyAnalysisQuery,
    ) -> Result<Option<Value>, HealthAnalysisError> {
        timeout_read(async {
            let mut txn = begin_daily_analysis(&self.work).await?;
            let result = query.read_in(&mut txn, context).await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
    }
}

async fn timeout_read<T>(
    operation: impl Future<Output = Result<T, HealthAnalysisError>>,
) -> Result<T, HealthAnalysisError> {
    tokio::time::timeout(Duration::from_millis(1500), operation)
        .await
        .map_err(|_| HealthAnalysisError::Database)?
}

async fn health_allowed(
    txn: &mut Transaction<'_, Postgres>,
    context: AnalysisSubject<'_>,
) -> Result<bool, HealthAnalysisError> {
    let channel = analysis_channel_in(txn, context)
        .await
        .map_err(|_| HealthAnalysisError::Database)?;
    Ok(channel.is_some() && onboarding_allows_model(txn, ModelCategories::HEALTH).await?)
}

fn parse_day(value: &str) -> Result<Date, HealthAnalysisError> {
    let day = Date::parse(value, &Iso8601::DATE).map_err(|_| HealthAnalysisError::DateRange)?;
    if day.to_string() != value {
        return Err(HealthAnalysisError::DateRange);
    }
    Ok(day)
}
