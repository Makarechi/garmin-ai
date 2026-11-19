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
    HealthAnalysisError, HealthPeriod, NapPolicy, begin_daily_analysis, daily_metric,
    main_sleep_timing_in, model_metric_packs, observed_values, owner_timezone, period_comparison,
    period_document, permits_timing_in, read_daily::model_packs_allowed,
    require_current_projection, sleep_analysis::sleep_analysis_in,
};
use crate::{
    Database,
    conversation_memory::{AnalysisSubject, analysis_channel_in},
    model_gate::onboarding::onboarding_allows_model,
};

pub const PERSONAL_BASELINE: &str = "personal_baseline";
pub const PERIOD_COMPARISON: &str = "analysis_compare_periods";
pub const SLEEP_ANALYSIS: &str = "analysis_sleep";

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
    #[serde(rename = "analysis_sleep")]
    Sleep {
        start: String,
        end: String,
        #[serde(default)]
        nap_policy: NapPolicy,
        #[serde(default)]
        timing_only: bool,
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
        configured_timezone: &str,
    ) -> Result<Option<Value>, HealthAnalysisError> {
        if !health_allowed(txn, context).await? {
            return Ok(None);
        }
        if let Self::Sleep {
            start,
            end,
            nap_policy,
            timing_only,
        } = self
        {
            let (start, end) = (parse_day(start)?, parse_day(end)?);
            if !permits_timing_in(txn).await? {
                return Ok(None);
            }
            let timezone = owner_timezone(txn, configured_timezone).await?;
            if *timing_only {
                if start != end || *nap_policy != NapPolicy::Separate {
                    return Err(HealthAnalysisError::SleepRange);
                }
                return main_sleep_timing_in(txn, start, &timezone).await.map(Some);
            }
            require_current_projection(txn).await?;
            return sleep_analysis_in(txn, start, end, &timezone, *nap_policy)
                .await
                .map(Some);
        }
        require_current_projection(txn).await?;
        let period = |start: &str, end: &str| HealthPeriod::new(parse_day(start)?, parse_day(end)?);
        let result = match self {
            Self::Baseline { metric, start, end } => {
                let (metric, sql) = daily_metric(metric)?;
                if !model_packs_allowed(txn, context.binding().owner_id, model_metric_packs(metric))
                    .await?
                {
                    return Ok(None);
                }
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
                metric,
                a_start,
                a_end,
                b_start,
                b_end,
            } => {
                let (metric, _) = daily_metric(metric)?;
                if !model_packs_allowed(txn, context.binding().owner_id, model_metric_packs(metric))
                    .await?
                {
                    return Ok(None);
                }
                period_comparison(
                    txn,
                    metric,
                    period(a_start, a_end)?,
                    period(b_start, b_end)?,
                )
                .await?
                .document()
            }
            Self::Sleep { .. } => return Err(HealthAnalysisError::SleepRange),
        };
        Ok(Some(result))
    }
}

#[derive(Deserialize, Serialize)]
pub struct DailyAnalysisEvidence {
    query: DailyAnalysisQuery,
    result_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    timezone: Option<String>,
}

impl DailyAnalysisEvidence {
    pub fn is_daily_tool(tool: &str) -> bool {
        matches!(tool, PERSONAL_BASELINE | PERIOD_COMPARISON | SLEEP_ANALYSIS)
    }

    pub(crate) fn single_sleep_timing(&self) -> bool {
        matches!(
            self.query,
            DailyAnalysisQuery::Sleep {
                timing_only: true,
                ..
            }
        )
    }

    pub fn from_read(evidence: &Value) -> Option<Self> {
        let arguments = evidence["arguments"].as_object()?;
        let query = DailyAnalysisQuery::from_arguments(evidence["tool"].as_str()?, arguments)?;
        let result = &evidence["result"];
        if !result.is_object() || result.get("error").is_some() {
            return None;
        }
        let timezone = if matches!(query, DailyAnalysisQuery::Sleep { .. }) {
            Some(result["timezone"].as_str()?.to_owned())
        } else {
            None
        };
        Some(Self {
            query,
            result_hash: result_hash(result)?,
            timezone,
        })
    }

    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        context: AnalysisSubject<'_>,
    ) -> Result<bool, HealthAnalysisError> {
        if matches!(self.query, DailyAnalysisQuery::Sleep { .. }) && self.timezone.is_none() {
            return Ok(false);
        }
        let value = self
            .query
            .read_in(txn, context, self.timezone.as_deref().unwrap_or("UTC"))
            .await?;
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
        configured_timezone: &str,
    ) -> Result<Option<Value>, HealthAnalysisError> {
        timeout_read(async {
            let mut txn = begin_daily_analysis(&self.work).await?;
            let result = query
                .read_in(&mut txn, context, configured_timezone)
                .await?;
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
    if !(1..=9999).contains(&day.year()) || day.to_string() != value {
        return Err(HealthAnalysisError::DateRange);
    }
    Ok(day)
}
