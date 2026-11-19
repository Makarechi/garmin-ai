use std::{collections::BTreeSet, time::Duration};

use ga_archive::json_spool::JsonDocument;
use ga_domain::{
    analysis_spec::AnalysisSpec, legacy_json::sum_numbers, threshold_time::local_hour_slices,
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use sqlx::Row;

use super::{
    interval,
    measurements::{MeasurementQuery, MeasurementSnapshot},
    rows::{ascii_json, floating},
};
use crate::health_analysis::generic::{GenericAnalysisError, GenericAnalysisReader};

#[derive(Clone)]
struct ThresholdInterval {
    left: i64,
    right: i64,
    left_display: String,
    right_display: String,
    value: f64,
    matches: bool,
}

struct ThresholdSnapshot {
    contract: Value,
    source: Option<String>,
    revisions: Map<String, Value>,
    intervals: Vec<ThresholdInterval>,
}

fn microseconds(at: time::OffsetDateTime) -> i64 {
    at.unix_timestamp() * 1_000_000 + i64::from(at.microsecond())
}

fn calendar(at: i64) -> Result<String, GenericAnalysisError> {
    StoredInstant::from_unix_microseconds(at)
        .map(|at| at.to_string())
        .map_err(|_| GenericAnalysisError::Calendar)
}

fn eligible(row: &MeasurementSnapshot, contract: &Value, id: uuid::Uuid) -> bool {
    row.value.is_finite()
        && row.quality == "observed"
        && !row.deleted
        && row.contract == Some(id)
        && contract["unit"].as_str() == Some(&row.unit)
        && contract["minimum"]
            .as_f64()
            .is_none_or(|minimum| row.value >= minimum)
        && contract["maximum"]
            .as_f64()
            .is_none_or(|maximum| row.value <= maximum)
}

impl GenericAnalysisReader {
    async fn threshold_snapshot(
        &mut self,
        spec: &AnalysisSpec,
    ) -> Result<ThresholdSnapshot, GenericAnalysisError> {
        let id = self.metric_contract(spec.document()).await?;
        let encoded: String = sqlx::query_scalar(
            "SELECT json_build_object('version',version,'value_kind',value_kind,'unit',unit,
             'scale_id',scale_id,'scale_version',scale_version,'minimum',minimum,'maximum',maximum,
             'coverage_policy',coverage_policy)::text FROM metric_definition_versions WHERE id=$1",
        )
        .bind(id)
        .fetch_one(&mut *self.txn)
        .await?;
        let contract: Value =
            serde_json::from_str(&encoded).map_err(|_| GenericAnalysisError::Database)?;
        if contract["value_kind"] != "physical_number"
            || contract["coverage_policy"]["kind"] != "time_weighted"
        {
            return Err(GenericAnalysisError::Value(
                "Threshold intervals require a time-weighted numeric metric",
            ));
        }
        let source = spec.document()["source"].as_str();
        if source.is_some_and(|source| !source.starts_with("measurement:")) {
            return Err(GenericAnalysisError::Value(
                "Threshold intervals currently require measurement samples",
            ));
        }
        let gap = contract["coverage_policy"]["max_gap_seconds"]
            .as_i64()
            .and_then(|gap| gap.checked_mul(1_000_000))
            .ok_or(GenericAnalysisError::Calendar)?;
        let start = microseconds(spec.start())
            .checked_sub(gap)
            .ok_or(GenericAnalysisError::Calendar)?;
        let end = microseconds(spec.end())
            .checked_add(gap)
            .ok_or(GenericAnalysisError::Calendar)?;
        calendar(start)?;
        calendar(end)?;
        let limit = spec.document()["limit"]
            .as_i64()
            .ok_or(GenericAnalysisError::Database)?;
        self.restore_display_timezone().await?;
        let mut sources = BTreeSet::new();
        let mut observation_count = 0;
        if source.is_none() {
            let observations = sqlx::query(include_str!("threshold_observation_sources.sql"))
                .bind(id)
                .bind(interval(start))
                .bind(interval(end))
                .bind(interval(spec.knowledge_cutoff_microseconds()))
                .bind(limit)
                .fetch_all(&mut *self.txn)
                .await?;
            observation_count = observations.len();
            for row in observations {
                if row.try_get::<bool, _>("invalid_snapshot")? {
                    return Err(GenericAnalysisError::InvalidSnapshot);
                }
                let event: Option<uuid::Uuid> = row.try_get("source_entry_id")?;
                let key = if event.is_some() {
                    "event".to_owned()
                } else {
                    let account: Option<String> = row.try_get("account")?;
                    let device: Option<String> = row.try_get("device")?;
                    format!("observation:{}", ascii_json(&json!([account, device]))?)
                };
                sources.insert(key);
            }
        }
        let mut rows = self
            .aggregate_measurement_snapshot(MeasurementQuery {
                contract: id,
                start: Some(start),
                end,
                cutoff: spec.knowledge_cutoff_microseconds(),
                source: source.and_then(|source| source.strip_prefix("measurement:")),
                limit: limit + 1,
                descending: false,
                include_invalid: true,
                retain_evidence: true,
            })
            .await?;
        if observation_count + rows.len()
            > usize::try_from(limit).map_err(|_| GenericAnalysisError::Database)?
        {
            return Err(GenericAnalysisError::Value(
                "Observation query exceeds its explicit result limit",
            ));
        }
        for row in &rows {
            sources.insert(format!("measurement:{}", row.source));
        }
        if sources.len() > 1 {
            return Err(GenericAnalysisError::Value(
                "Multiple metric sources; select one source",
            ));
        }
        if sources
            .iter()
            .any(|source| !source.starts_with("measurement:"))
        {
            return Err(GenericAnalysisError::Value(
                "Threshold intervals currently require measurement samples",
            ));
        }
        let selected = sources
            .into_iter()
            .next()
            .or_else(|| source.map(str::to_owned));
        let mut revisions = Map::new();
        for row in &rows {
            if let Some((reference, token)) = &row.evidence {
                revisions.insert(reference.clone(), Value::String(token.clone()));
            }
        }
        rows.sort_by_cached_key(|row| {
            (
                row.observed_local,
                format!(
                    "measurement:{}:{}:{}",
                    row.metric, row.source, row.observed_display
                ),
            )
        });
        let threshold = spec.document()["threshold"]
            .as_f64()
            .ok_or(GenericAnalysisError::Database)?;
        let mut intervals = Vec::new();
        for pair in rows.windows(2) {
            let (first, second) = (&pair[0], &pair[1]);
            let duration = second.observed - first.observed;
            if !eligible(first, &contract, id)
                || !eligible(second, &contract, id)
                || second.observed > spec.knowledge_cutoff_microseconds()
                || duration <= 0
                || duration > gap
            {
                continue;
            }
            let left = microseconds(spec.start()).max(first.observed);
            let right = microseconds(spec.end()).min(second.observed);
            if left >= right {
                continue;
            }
            let matches = match spec.document()["threshold_operator"].as_str() {
                Some("gt") => first.value > threshold,
                Some("gte") => first.value >= threshold,
                Some("lt") => first.value < threshold,
                Some("lte") => first.value <= threshold,
                _ => return Err(GenericAnalysisError::Database),
            };
            intervals.push(ThresholdInterval {
                left,
                right,
                left_display: if left == microseconds(spec.start()) {
                    calendar(left)?
                } else {
                    first.observed_display.clone()
                },
                right_display: if right == microseconds(spec.end()) {
                    calendar(right)?
                } else {
                    second.observed_display.clone()
                },
                value: first.value,
                matches,
            });
        }
        Ok(ThresholdSnapshot {
            contract,
            source: selected,
            revisions,
            intervals,
        })
    }

    pub async fn threshold_segments(
        mut self,
        spec: &AnalysisSpec,
    ) -> Result<JsonDocument, GenericAnalysisError> {
        let work = async {
            self.prepare(spec).await?;
            let snapshot = self.threshold_snapshot(spec).await?;
            let durations: Vec<Value> = snapshot
                .intervals
                .iter()
                .map(|row| floating((row.right - row.left) as f64 / 1_000_000.0))
                .collect::<Result<_, _>>()?;
            let covered =
                sum_numbers(durations.iter()).map_err(|_| GenericAnalysisError::Database)?;
            let covered = covered.as_f64().ok_or(GenericAnalysisError::Database)?;
            let mut matching = 0.0;
            let mut segments: Vec<ThresholdInterval> = Vec::new();
            for row in &snapshot.intervals {
                if !row.matches {
                    continue;
                }
                matching += (row.right - row.left) as f64 / 1_000_000.0;
                if let Some(last) = segments.last_mut().filter(|last| last.right == row.left) {
                    last.right = row.right;
                    last.right_display = row.right_display.clone();
                    if row.value > last.value {
                        last.value = row.value;
                    }
                } else {
                    segments.push(row.clone());
                }
            }
            let segments: Vec<Value> = segments
                .into_iter()
                .map(|row| {
                    json!({
                        "start": row.left_display,
                        "end": row.right_display,
                        "duration_seconds": (row.right-row.left) as f64/1_000_000.0,
                        "peak": row.value,
                    })
                })
                .collect();
            let mut payload = threshold_metadata(spec, &snapshot);
            payload["covered_seconds"] = json!(covered);
            payload["coverage_ratio"] = json!(
                covered
                    / ((microseconds(spec.end()) - microseconds(spec.start())) as f64
                        / 1_000_000.0)
            );
            payload["matching_seconds"] = if covered != 0.0 {
                json!(matching)
            } else {
                Value::Null
            };
            payload["segments"] = json!(segments);
            payload["limitations"] = json!([
                "Only intervals between consecutive valid measurements within the maximum gap are covered",
                "Missing time is unknown, not below threshold",
                "Measurements and sources are not combined",
            ]);
            self.finish_aggregate_document(payload).await
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| GenericAnalysisError::Database)?
    }

    pub async fn threshold_by_local_hour(
        mut self,
        spec: &AnalysisSpec,
    ) -> Result<JsonDocument, GenericAnalysisError> {
        let work = async {
            self.prepare(spec).await?;
            let snapshot = self.threshold_snapshot(spec).await?;
            let zone = spec.document()["timezone"]
                .as_str()
                .ok_or(GenericAnalysisError::Database)?;
            let mut scheduled = [0.0; 24];
            let mut covered = [0.0; 24];
            let mut matching = [0.0; 24];
            let mut days: [BTreeSet<_>; 24] = std::array::from_fn(|_| BTreeSet::new());
            for slice in
                local_hour_slices(microseconds(spec.start()), microseconds(spec.end()), zone)
                    .map_err(|_| GenericAnalysisError::Calendar)?
            {
                scheduled[slice.hour] += slice.seconds;
            }
            for row in &snapshot.intervals {
                for slice in local_hour_slices(row.left, row.right, zone)
                    .map_err(|_| GenericAnalysisError::Calendar)?
                {
                    covered[slice.hour] += slice.seconds;
                    days[slice.hour].insert(slice.day);
                    if row.matches {
                        matching[slice.hour] += slice.seconds;
                    }
                }
            }
            let rows: Vec<Value> = (0..24)
                .map(|hour| {
                    json!({
                        "hour": hour,
                        "scheduled_seconds": scheduled[hour],
                        "covered_seconds": covered[hour],
                        "matching_seconds": if covered[hour]!=0.0 {Some(matching[hour])} else {None},
                        "matching_ratio": if covered[hour]!=0.0 {Some(matching[hour]/covered[hour])} else {None},
                        "coverage_ratio": if scheduled[hour]!=0.0 {Some(covered[hour]/scheduled[hour])} else {None},
                        "observed_days": days[hour].len(),
                    })
                })
                .collect();
            let mut payload = threshold_metadata(spec, &snapshot);
            payload["timezone"] = spec.document()["timezone"].clone();
            payload["rows"] = json!(rows);
            payload["limitations"] = json!([
                "Only intervals between consecutive valid measurements within the maximum gap are covered",
                "Matching share uses observed time, not sample count or unobserved time",
                "Measurements and sources are not combined",
            ]);
            self.finish_aggregate_document(payload).await
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| GenericAnalysisError::Database)?
    }
}

fn threshold_metadata(spec: &AnalysisSpec, snapshot: &ThresholdSnapshot) -> Value {
    let cutoff = spec.document()["knowledge_cutoff"]
        .as_str()
        .unwrap_or_default();
    let cutoff = cutoff
        .strip_suffix('Z')
        .map(|value| format!("{value}+00:00"))
        .unwrap_or_else(|| cutoff.to_owned());
    json!({"spec_hash":spec.hash(),"metric":spec.document()["metric_key"],
        "metric_version":snapshot.contract["version"],"unit":snapshot.contract["unit"],
        "scale_id":snapshot.contract["scale_id"],"scale_version":snapshot.contract["scale_version"],
        "source":snapshot.source,"threshold":spec.document()["threshold"],
        "threshold_operator":spec.document()["threshold_operator"],
        "input_revisions":snapshot.revisions,"knowledge_cutoff":cutoff,
    })
}
