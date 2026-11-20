use std::{io::Read, time::Duration};

use ga_archive::json_spool::JsonDocument;
use ga_domain::{
    legacy_json::{Utf8Layout, encode, encode_ascii},
    model_consent::ModelCategories,
    sha256_hex,
    statistics::describe,
    timestamp::StoredInstant,
};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Number, Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, format_description::well_known::Iso8601};

use super::{
    ActivityReadError, DeviceHistoryError, HealthAnalysisError, HealthPeriod, MODEL_HEALTH_PACKS,
    NapPolicy, SeriesReadError,
    activity_reads::{model_activity_details_in, model_activity_summaries_in},
    begin_daily_analysis,
    coffee::coffee_sleep_in,
    daily_metric,
    device_history::{device_history_in, device_range},
    event_windows::{event_windows_in, permits_model_event_kind_in},
    freshness_report::model_freshness_in,
    insights::insights_in,
    lagged::lagged_association_in,
    main_sleep_timing_in,
    migraine::migraine_comparison_in,
    model_metric_packs, observed_values, owner_timezone, period_comparison, period_document,
    permits_timing_in,
    read_daily::{model_packs_allowed, range_in, snapshot_in},
    require_current_projection,
    running::running_efficiency_in,
    series::metric_series_in,
    sleep_analysis::sleep_analysis_in,
    stress::stress_hours_in,
};
use crate::{
    Database,
    conversation_memory::{AnalysisSubject, analysis_channel_in},
    diary::{
        DiaryReadError,
        wellbeing::{require_model_wellbeing_in, wellbeing_observations_in},
    },
    model_gate::onboarding::onboarding_allows_model,
};

pub const PERSONAL_BASELINE: &str = "personal_baseline";
pub const PERIOD_COMPARISON: &str = "analysis_compare_periods";
pub const SLEEP_ANALYSIS: &str = "analysis_sleep";
pub const DATA_FRESHNESS: &str = "data_freshness";
pub const LAGGED_ASSOCIATION: &str = "analysis_lagged_association";
pub const HEALTH_SNAPSHOT: &str = "health_snapshot";
pub const HEALTH_RANGE: &str = "health_range";
pub const EVENT_WINDOWS: &str = "analysis_event_windows";
pub const COFFEE_SLEEP: &str = "analysis_coffee_sleep";
const COFFEE_MODEL_PACKS: &[&str] = &["caffeine", "sleep"];
pub const METRIC_SERIES: &str = "metric_series";
pub const RUNNING_EFFICIENCY: &str = "analysis_running_efficiency";
const RUNNING_MODEL_PACKS: &[&str] = &["training", "sleep", "wellbeing"];
pub const STRESS_HOURS: &str = "analysis_stress_by_hour";
pub const MIGRAINE_WINDOWS: &str = "analysis_migraine_windows";
pub const WELLBEING_OBSERVATIONS: &str = "wellbeing_observations";
pub const DEVICE_HISTORY: &str = "device_history";
pub const ACTIVITIES: &str = "activities";
pub const ACTIVITY_DETAILS: &str = "activity_details";
pub const INSIGHTS_LIST: &str = "insights_list";
const MODEL_INSIGHT_PACKS: &[&str] = &["sleep", "wellbeing", "migraine"];
const MAX_MODEL_RESULT_CHARS: usize = 24_000;
const MAX_MODEL_RESULT_BYTES: usize = MAX_MODEL_RESULT_CHARS * 4;

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
    #[serde(rename = "data_freshness")]
    Freshness {},
    #[serde(rename = "analysis_lagged_association")]
    Lagged {
        metric_a: String,
        metric_b: String,
        start: String,
        end: String,
        lags: Vec<i64>,
    },
    #[serde(rename = "analysis_event_windows")]
    EventWindows {
        event_type: String,
        metric: String,
        start: String,
        end: String,
    },
    #[serde(rename = "analysis_coffee_sleep")]
    CoffeeSleep {
        start: String,
        end: String,
        #[serde(
            default,
            deserialize_with = "explicit_number",
            skip_serializing_if = "Option::is_none"
        )]
        late_hours: Option<Number>,
        #[serde(default = "default_coffee_outcome")]
        outcome: String,
    },
    #[serde(rename = "health_snapshot")]
    Snapshot { day: String },
    #[serde(rename = "health_range")]
    Range { start: String, end: String },
    #[serde(rename = "metric_series")]
    Series {
        metric: String,
        start: String,
        end: String,
        #[serde(default = "default_series_minutes")]
        minutes: u16,
    },
    #[serde(rename = "analysis_running_efficiency")]
    RunningEfficiency {
        start: String,
        end: String,
        #[serde(default = "default_hr_min")]
        hr_min: Number,
        #[serde(default = "default_hr_max")]
        hr_max: Number,
    },
    #[serde(rename = "analysis_stress_by_hour")]
    StressHours {
        start: String,
        end: String,
        timezone: Option<String>,
    },
    #[serde(rename = "wellbeing_observations")]
    Wellbeing {
        start: Value,
        end: Value,
        cursor: Option<String>,
    },
    #[serde(rename = "device_history")]
    DeviceHistory {
        start: Value,
        end: Value,
        #[serde(default = "default_activity_limit")]
        limit: u16,
    },
    #[serde(rename = "activities")]
    Activities {
        start: Value,
        end: Value,
        kind: Option<String>,
    },
    #[serde(rename = "activity_details")]
    ActivityDetails {
        activity_id: String,
        #[serde(default)]
        include_samples: bool,
        #[serde(default)]
        offset: u32,
        #[serde(default = "default_activity_limit")]
        limit: u16,
    },
    #[serde(rename = "insights_list")]
    Insights {
        #[serde(default = "default_insight_limit")]
        limit: u16,
        cursor: Option<String>,
    },
    #[serde(rename = "analysis_migraine_windows")]
    Migraine {
        metric: String,
        start: String,
        end: String,
        timezone: Option<String>,
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
        checked_at: Option<StoredInstant>,
    ) -> Result<Option<Value>, HealthAnalysisError> {
        if let Self::Wellbeing { start, end, cursor } = self {
            if !wellbeing_allowed(txn, context).await? {
                return Ok(None);
            }
            return wellbeing_observations_in(
                txn,
                parse_instant(start)?,
                parse_instant(end)?,
                cursor.as_deref(),
            )
            .await
            .map(Some)
            .map_err(|error| match error {
                DiaryReadError::Database => HealthAnalysisError::Database,
                _ => HealthAnalysisError::DateRange,
            });
        }
        if !health_allowed(txn, context).await? {
            return Ok(None);
        }
        if matches!(self, Self::Freshness {}) {
            let Some(at) = checked_at else {
                return Ok(None);
            };
            let timezone = owner_timezone(txn, configured_timezone).await?;
            return model_freshness_in(txn, context.binding().owner_id, at, &timezone)
                .await
                .map(|(report, _)| Some(report));
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
        if matches!(
            self,
            Self::Activities { .. } | Self::ActivityDetails { .. } | Self::DeviceHistory { .. }
        ) && !model_packs_allowed(txn, context.binding().owner_id, &["training"]).await?
        {
            return Ok(None);
        }
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
            Self::Lagged {
                metric_a,
                metric_b,
                start,
                end,
                lags,
            } => {
                let (metric_a, _) = daily_metric(metric_a)?;
                let (metric_b, _) = daily_metric(metric_b)?;
                let owner = context.binding().owner_id;
                if !model_packs_allowed(txn, owner, model_metric_packs(metric_a)).await?
                    || !model_packs_allowed(txn, owner, model_metric_packs(metric_b)).await?
                {
                    return Ok(None);
                }
                lagged_association_in(txn, metric_a, metric_b, period(start, end)?, lags).await?
            }
            Self::EventWindows {
                event_type,
                metric,
                start,
                end,
            } => {
                let kind = event_type.strip_prefix("system.").unwrap_or(event_type);
                if !permits_model_event_kind_in(txn, kind).await?
                    || !model_packs_allowed(
                        txn,
                        context.binding().owner_id,
                        model_metric_packs(metric),
                    )
                    .await?
                {
                    return Ok(None);
                }
                let parse = |value: &str| {
                    StoredInstant::legacy_input_with_offset(&json!(value))
                        .ok_or(HealthAnalysisError::InvalidTemporalTimestamp)
                };
                let output = event_windows_in(txn, kind, metric, parse(start)?, parse(end)?)
                    .await
                    .map_err(model_series_error)?;
                compact_document(output)?
            }
            Self::CoffeeSleep {
                start,
                end,
                late_hours,
                outcome,
            } => {
                if !onboarding_allows_model(txn, ModelCategories::DIARY).await?
                    || !model_packs_allowed(txn, context.binding().owner_id, COFFEE_MODEL_PACKS)
                        .await?
                {
                    return Ok(None);
                }
                let hours = late_hours
                    .as_ref()
                    .map(|value| value.as_f64().ok_or(HealthAnalysisError::DateRange))
                    .transpose()?;
                coffee_sleep_in(txn, parse_day(start)?, parse_day(end)?, hours, outcome).await?
            }
            Self::Snapshot { day } => {
                if !model_packs_allowed(txn, context.binding().owner_id, MODEL_HEALTH_PACKS).await?
                {
                    return Ok(None);
                }
                snapshot_in(txn, parse_day(day)?).await?
            }
            Self::Range { start, end } => {
                if !model_packs_allowed(txn, context.binding().owner_id, MODEL_HEALTH_PACKS).await?
                {
                    return Ok(None);
                }
                range_in(txn, period(start, end)?).await?
            }
            Self::Series {
                metric,
                start,
                end,
                minutes,
            } => {
                if !model_packs_allowed(txn, context.binding().owner_id, model_metric_packs(metric))
                    .await?
                {
                    return Ok(None);
                }
                let parse = |value: &str| {
                    StoredInstant::legacy_input_with_offset(&json!(value))
                        .ok_or(HealthAnalysisError::InvalidTemporalTimestamp)
                };
                metric_series_in(txn, metric, parse(start)?, parse(end)?, *minutes)
                    .await
                    .map_err(model_series_error)?
            }
            Self::RunningEfficiency {
                start,
                end,
                hr_min,
                hr_max,
            } => {
                if !model_packs_allowed(txn, context.binding().owner_id, RUNNING_MODEL_PACKS)
                    .await?
                {
                    return Ok(None);
                }
                let parse = |value: &str| {
                    StoredInstant::from_python_isoformat(value)
                        .ok_or(HealthAnalysisError::InvalidTemporalTimestamp)
                };
                let min = hr_min.as_f64().ok_or(HealthAnalysisError::DateRange)?;
                let max = hr_max.as_f64().ok_or(HealthAnalysisError::DateRange)?;
                running_efficiency_in(txn, parse(start)?, parse(end)?, min, max)
                    .await
                    .map_err(|error| match error {
                        ActivityReadError::Analysis(error) => error,
                        _ => HealthAnalysisError::DateRange,
                    })?
            }
            Self::StressHours {
                start,
                end,
                timezone,
            } => {
                if !model_packs_allowed(txn, context.binding().owner_id, &["wellbeing"]).await? {
                    return Ok(None);
                }
                stress_hours_in(
                    txn,
                    parse_day(start)?,
                    parse_day(end)?,
                    timezone.as_deref(),
                    configured_timezone,
                )
                .await
                .map_err(model_series_error)?
            }
            Self::DeviceHistory { start, end, limit } => {
                let map_error = |error| match error {
                    DeviceHistoryError::Analysis(error) => error,
                    _ => HealthAnalysisError::DateRange,
                };
                let (start, end) = device_range(parse_instant(start)?, parse_instant(end)?, *limit)
                    .map_err(map_error)?;
                device_history_in(txn, start, end, *limit)
                    .await
                    .map_err(map_error)?
            }
            Self::Activities { start, end, kind } => model_activity_result(
                model_activity_summaries_in(
                    txn,
                    parse_instant(start)?,
                    parse_instant(end)?,
                    kind.as_deref(),
                )
                .await,
            )?,
            Self::ActivityDetails {
                activity_id,
                include_samples,
                offset,
                limit,
            } => model_activity_result(
                model_activity_details_in(txn, activity_id, *include_samples, *offset, *limit)
                    .await,
            )?,
            Self::Insights { limit, cursor } => {
                if !onboarding_allows_model(txn, ModelCategories::DIARY).await?
                    || !model_packs_allowed(txn, context.binding().owner_id, MODEL_INSIGHT_PACKS)
                        .await?
                {
                    return Ok(None);
                }
                if let Some(document) =
                    insights_in(txn, *limit, cursor.as_deref(), Some(MAX_MODEL_RESULT_BYTES))
                        .await?
                {
                    compact_document(document)?
                } else {
                    oversized_model_result()
                }
            }
            Self::Migraine {
                metric,
                start,
                end,
                timezone,
            } => {
                let (metric, _) = daily_metric(metric)?;
                let owner = context.binding().owner_id;
                if !onboarding_allows_model(txn, ModelCategories::DIARY).await?
                    || !model_packs_allowed(txn, owner, &["migraine"]).await?
                    || !model_packs_allowed(txn, owner, model_metric_packs(metric)).await?
                {
                    return Ok(None);
                }
                migraine_comparison_in(
                    txn,
                    metric,
                    period(start, end)?,
                    timezone.as_deref(),
                    configured_timezone,
                )
                .await?
            }
            Self::Sleep { .. } => return Err(HealthAnalysisError::SleepRange),
            Self::Freshness {} | Self::Wellbeing { .. } => return Ok(None),
        };
        Ok(Some(result))
    }
    fn result_hash(&self, value: &Value) -> Option<String> {
        if !matches!(self, Self::RunningEfficiency { .. }) {
            return result_hash(value);
        }
        let mut snapshot = value.clone();
        // Keep all selected observation identities and values. The query clock labels
        // when the read happened and will differ even when those observations still match.
        for row in snapshot["rows"].as_array_mut()? {
            for feature in row["context"].as_object_mut()?.values_mut() {
                feature.as_object_mut()?.remove("knowledge_cutoff");
            }
        }
        result_hash(&snapshot)
    }
}

#[derive(Deserialize, Serialize)]
pub struct DailyAnalysisEvidence {
    query: DailyAnalysisQuery,
    result_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    timezone: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    checked_at: Option<String>,
}

impl DailyAnalysisEvidence {
    pub fn is_daily_tool(tool: &str) -> bool {
        matches!(
            tool,
            PERSONAL_BASELINE
                | PERIOD_COMPARISON
                | SLEEP_ANALYSIS
                | DATA_FRESHNESS
                | LAGGED_ASSOCIATION
                | MIGRAINE_WINDOWS
                | STRESS_HOURS
                | RUNNING_EFFICIENCY
                | HEALTH_SNAPSHOT
                | HEALTH_RANGE
                | METRIC_SERIES
                | COFFEE_SLEEP
                | EVENT_WINDOWS
                | WELLBEING_OBSERVATIONS
                | DEVICE_HISTORY
                | ACTIVITIES
                | ACTIVITY_DETAILS
                | INSIGHTS_LIST
        )
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

    pub fn from_read(evidence: &Value, timezone: &str) -> Option<Self> {
        let arguments = evidence["arguments"].as_object()?;
        let query = DailyAnalysisQuery::from_arguments(evidence["tool"].as_str()?, arguments)?;
        let result = &evidence["result"];
        if !result.is_object() || result.get("error").is_some() {
            return None;
        }
        let (timezone, checked_at) = match &query {
            DailyAnalysisQuery::Sleep { .. } => (Some(result["timezone"].as_str()?.into()), None),
            DailyAnalysisQuery::Migraine { .. } | DailyAnalysisQuery::StressHours { .. } => {
                (Some(timezone.into()), None)
            }
            DailyAnalysisQuery::Freshness {} => {
                let checked_at = result["checked_at"].as_str()?;
                StoredInstant::from_python_isoformat(checked_at)?;
                (Some(timezone.into()), Some(checked_at.into()))
            }
            _ => (None, None),
        };
        let result_hash = query.result_hash(result)?;
        Some(Self {
            query,
            result_hash,
            timezone,
            checked_at,
        })
    }

    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        context: AnalysisSubject<'_>,
    ) -> Result<bool, HealthAnalysisError> {
        if matches!(
            self.query,
            DailyAnalysisQuery::Sleep { .. }
                | DailyAnalysisQuery::Freshness {}
                | DailyAnalysisQuery::Migraine { .. }
                | DailyAnalysisQuery::StressHours { .. }
        ) && self.timezone.is_none()
        {
            return Ok(false);
        }
        let checked_at = self
            .checked_at
            .as_deref()
            .and_then(StoredInstant::from_python_isoformat);
        let value = self
            .query
            .read_in(
                txn,
                context,
                self.timezone.as_deref().unwrap_or("UTC"),
                checked_at,
            )
            .await?;
        Ok(value
            .as_ref()
            .and_then(|value| self.query.result_hash(value))
            .as_deref()
            == Some(&self.result_hash))
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

pub struct DailyAnalysisAccess {
    pub migraine: bool,
    pub sleep: bool,
    pub wellbeing: bool,
    pub running: bool,
    pub summaries: bool,
    pub coffee_sleep: bool,
    pub training: bool,
    pub insights: bool,
}

impl Database {
    pub async fn analysis_wellbeing_access(&self, context: AnalysisSubject<'_>) -> bool {
        matches!(
            timeout_read(async {
                let mut txn = begin_daily_analysis(&self.work).await?;
                let allowed = wellbeing_allowed(&mut txn, context).await?;
                txn.commit().await?;
                Ok(allowed)
            })
            .await,
            Ok(true)
        )
    }

    pub async fn analysis_health_access(
        &self,
        context: AnalysisSubject<'_>,
    ) -> Option<DailyAnalysisAccess> {
        timeout_read(async {
            let mut txn = begin_daily_analysis(&self.work).await?;
            if !health_allowed(&mut txn, context).await? {
                return Ok(None);
            }
            let owner = context.binding().owner_id;
            let diary = onboarding_allows_model(&mut txn, ModelCategories::DIARY).await?;
            let migraine = diary && model_packs_allowed(&mut txn, owner, &["migraine"]).await?;
            let sleep = model_packs_allowed(&mut txn, owner, &["sleep"]).await?;
            let wellbeing = model_packs_allowed(&mut txn, owner, &["wellbeing"]).await?;
            let training = model_packs_allowed(&mut txn, owner, &["training"]).await?;
            let running = wellbeing && training && sleep;
            let summaries = model_packs_allowed(&mut txn, owner, MODEL_HEALTH_PACKS).await?;
            let coffee_sleep =
                diary && model_packs_allowed(&mut txn, owner, COFFEE_MODEL_PACKS).await?;
            let insights = migraine && wellbeing && sleep;
            txn.commit().await?;
            Ok(Some(DailyAnalysisAccess {
                migraine,
                sleep: diary && sleep,
                wellbeing,
                running,
                summaries,
                coffee_sleep,
                training,
                insights,
            }))
        })
        .await
        .ok()?
    }

    pub async fn read_analysis_daily(
        &self,
        context: AnalysisSubject<'_>,
        query: &DailyAnalysisQuery,
        configured_timezone: &str,
        at: StoredInstant,
    ) -> Result<Option<Value>, HealthAnalysisError> {
        timeout_read(async {
            let mut txn = begin_daily_analysis(&self.work).await?;
            let result = query
                .read_in(&mut txn, context, configured_timezone, Some(at))
                .await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
    }

    pub async fn read_analysis_quality(
        &self,
        context: AnalysisSubject<'_>,
        configured_timezone: &str,
        at: StoredInstant,
    ) -> Result<Option<(Value, bool)>, HealthAnalysisError> {
        timeout_read(async {
            let mut txn = begin_daily_analysis(&self.work).await?;
            if !health_allowed(&mut txn, context).await? {
                return Ok(None);
            }
            let timezone = owner_timezone(&mut txn, configured_timezone).await?;
            let report =
                model_freshness_in(&mut txn, context.binding().owner_id, at, &timezone).await?;
            txn.commit().await?;
            Ok(Some(report))
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

async fn wellbeing_allowed(
    txn: &mut Transaction<'_, Postgres>,
    context: AnalysisSubject<'_>,
) -> Result<bool, HealthAnalysisError> {
    let channel = analysis_channel_in(txn, context)
        .await
        .map_err(|_| HealthAnalysisError::Database)?;
    if channel.is_none() {
        return Ok(false);
    }
    match require_model_wellbeing_in(txn, context.binding().owner_id).await {
        Ok(()) => Ok(true),
        Err(DiaryReadError::Database) => Err(HealthAnalysisError::Database),
        Err(_) => Ok(false),
    }
}

fn parse_day(value: &str) -> Result<Date, HealthAnalysisError> {
    let day = Date::parse(value, &Iso8601::DATE).map_err(|_| HealthAnalysisError::DateRange)?;
    if !(1..=9999).contains(&day.year()) || day.to_string() != value {
        return Err(HealthAnalysisError::DateRange);
    }
    Ok(day)
}

fn default_hr_min() -> Number {
    0.into()
}

fn default_hr_max() -> Number {
    250.into()
}

fn parse_instant(value: &Value) -> Result<StoredInstant, HealthAnalysisError> {
    StoredInstant::from_legacy_input(value).ok_or(HealthAnalysisError::InvalidTemporalTimestamp)
}

fn model_activity_result(
    result: Result<Value, ActivityReadError>,
) -> Result<Value, HealthAnalysisError> {
    match result {
        Ok(value) => Ok(value),
        Err(ActivityReadError::ResultTooLarge) => Ok(oversized_model_result()),
        Err(ActivityReadError::Analysis(error)) => Err(error),
        Err(_) => Err(HealthAnalysisError::DateRange),
    }
}

fn oversized_model_result() -> Value {
    json!({"error":"result_too_large",
        "instruction":"Narrow the date range or use an analysis tool. Do not treat this as empty data."})
}

pub(crate) fn compact_document(document: JsonDocument) -> Result<Value, HealthAnalysisError> {
    if document.byte_len() > MAX_MODEL_RESULT_BYTES as u64 {
        return Ok(oversized_model_result());
    }
    let mut bytes = Vec::with_capacity(document.byte_len() as usize);
    document
        .reader()
        .read_to_end(&mut bytes)
        .map_err(|_| HealthAnalysisError::Database)?;
    let encoded = encode(&bytes, Utf8Layout::Ordered, MAX_MODEL_RESULT_BYTES)
        .map_err(|_| HealthAnalysisError::Database)?;
    if std::str::from_utf8(&encoded)
        .map_err(|_| HealthAnalysisError::Database)?
        .chars()
        .count()
        > MAX_MODEL_RESULT_CHARS
    {
        return Ok(oversized_model_result());
    }
    serde_json::from_slice(&bytes).map_err(|_| HealthAnalysisError::Database)
}

fn default_insight_limit() -> u16 {
    30
}

fn default_activity_limit() -> u16 {
    100
}

fn default_series_minutes() -> u16 {
    5
}

fn model_series_error(error: SeriesReadError) -> HealthAnalysisError {
    match error {
        SeriesReadError::Analysis(error) => error,
        SeriesReadError::Invalid | SeriesReadError::SampleLimit => HealthAnalysisError::DateRange,
    }
}

fn explicit_number<'de, D>(deserializer: D) -> Result<Option<Number>, D::Error>
where
    D: Deserializer<'de>,
{
    Number::deserialize(deserializer).map(Some)
}

fn default_coffee_outcome() -> String {
    "sleep_score".into()
}
