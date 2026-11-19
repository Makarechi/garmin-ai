mod composition;
mod measurements;
mod rows;
mod thresholds;

use std::{
    collections::{BTreeSet, HashMap, HashSet},
    time::Duration,
};

use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::{
    analysis_spec::{AnalysisSpec, DerivedAnalysisSpec},
    metric_aggregate::{MetricSample, aggregate_samples},
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use sqlx_postgres::types::PgInterval;
use uuid::Uuid;

use super::{GenericAnalysisError, GenericAnalysisReader, source::SourceSelection};
use measurements::{MeasurementIdentity, MeasurementQuery};
use rows::AggregateRow;

struct AggregateContract {
    id: Uuid,
    document: Value,
    value_kind: String,
    time_semantics: String,
    method: String,
    gap: Option<i64>,
}

struct AggregateWindow {
    start: i64,
    end: i64,
    cutoff: i64,
    predecessor_start: i64,
    measurement_start: i64,
}

fn interval(microseconds: i64) -> PgInterval {
    PgInterval {
        months: 0,
        days: 0,
        microseconds,
    }
}

impl AggregateContract {
    fn new(
        id: Uuid,
        document: Value,
        spec: &DerivedAnalysisSpec,
    ) -> Result<Self, GenericAnalysisError> {
        let time_semantics = document["time_semantics"]
            .as_str()
            .ok_or(GenericAnalysisError::Database)?
            .to_owned();
        if time_semantics == "calendar_period" {
            return Err(GenericAnalysisError::Value(
                "Calendar-period metric windows are not supported",
            ));
        }
        let method = spec.document()["method"]
            .as_str()
            .or_else(|| document["aggregation"].as_str())
            .ok_or(GenericAnalysisError::Database)?
            .to_owned();
        let allowed = document["allowed_methods"]
            .as_array()
            .ok_or(GenericAnalysisError::Database)?;
        if !allowed.iter().any(|value| value.as_str() == Some(&method)) {
            return Err(GenericAnalysisError::Value(
                "Aggregation is not allowed by this metric version",
            ));
        }
        let value_kind = document["value_kind"]
            .as_str()
            .ok_or(GenericAnalysisError::Database)?
            .to_owned();
        let gap = if document["coverage_policy"]["kind"] == "time_weighted" {
            Some(
                document["coverage_policy"]["max_gap_seconds"]
                    .as_i64()
                    .ok_or(GenericAnalysisError::Database)?,
            )
        } else {
            None
        };
        Ok(Self {
            id,
            document,
            value_kind,
            time_semantics,
            method,
            gap,
        })
    }
}

impl AggregateWindow {
    fn new(
        spec: &DerivedAnalysisSpec,
        contract: &AggregateContract,
    ) -> Result<Self, GenericAnalysisError> {
        let start = spec.start_microseconds();
        let end = spec.end_microseconds();
        let predecessor_start = match contract.gap {
            Some(gap) => spec
                .start_before_seconds(gap)
                .map_err(|error| match error {
                    ga_domain::analysis_spec::InvalidAnalysisSpec::CalendarOverflow => {
                        GenericAnalysisError::Calendar
                    }
                    ga_domain::analysis_spec::InvalidAnalysisSpec::Validation => {
                        GenericAnalysisError::Database
                    }
                })?,
            None => start,
        };
        Ok(Self {
            start,
            end,
            cutoff: spec.knowledge_cutoff_microseconds(),
            predecessor_start,
            measurement_start: if contract.time_semantics == "interval" {
                predecessor_start
            } else {
                start
            },
        })
    }
}

fn interval_ends(
    rows: &[AggregateRow],
    gap: Option<i64>,
) -> Result<HashMap<String, i64>, GenericAnalysisError> {
    let Some(gap) = gap else {
        return Ok(HashMap::new());
    };
    let gap = gap
        .checked_mul(1000000)
        .ok_or(GenericAnalysisError::Calendar)?;
    let mut ends = HashMap::with_capacity(rows.len());
    for (index, row) in rows.iter().enumerate() {
        let maximum = row
            .observed
            .checked_add(gap)
            .filter(|at| StoredInstant::from_unix_microseconds(*at).is_ok())
            .ok_or(GenericAnalysisError::Calendar)?;
        let next = rows.get(index + 1).map(|following| following.observed);
        let end = if let Some(end) = row.effective_end {
            end
        } else if row
            .effective_start
            .is_some_and(|start| start < row.observed)
        {
            row.observed
        } else {
            next.map_or(maximum, |next| next.min(maximum))
        };
        // Legacy measurements share their historical synthetic ID. Keep the same
        // final interval bound for every occurrence of that retained ID.
        ends.insert(row.id.clone(), end);
    }
    Ok(ends)
}

impl GenericAnalysisReader {
    async fn aggregate_predecessor(
        &mut self,
        contract: &AggregateContract,
        window: &AggregateWindow,
        selected: &SourceSelection,
        original_measurement_source: Option<&str>,
    ) -> Result<Option<Value>, GenericAnalysisError> {
        let mut candidates: Vec<(i64, i64, Value)> = self
            .aggregate_observations(contract, window, selected, true)
            .await?
            .into_iter()
            .map(|row| (row.observed, row.ingested, row.value))
            .collect();
        if matches!(selected.mode, "all" | "measurement") {
            candidates.extend(
                self.aggregate_measurement_snapshot(MeasurementQuery {
                    contract: contract.id,
                    start: None,
                    end: window.start,
                    cutoff: window.cutoff,
                    source: original_measurement_source,
                    limit: 1,
                    descending: true,
                    include_invalid: false,
                    retain_evidence: false,
                })
                .await?
                .into_iter()
                .map(|row| Ok((row.observed, row.ingested, rows::floating(row.value)?)))
                .collect::<Result<Vec<_>, GenericAnalysisError>>()?,
            );
        }
        let supplemental = self
            .aggregate_measurement_fallback(contract.id, window, selected, true)
            .await?;
        // Python considers the current projection before retained history on ties.
        for historical in [false, true] {
            candidates.extend(
                supplemental
                    .iter()
                    .filter(|row| row.historical == historical)
                    .map(|row| (row.row.observed, row.row.ingested, row.row.value.clone())),
            );
        }
        let mut best: Option<(i64, i64, Value)> = None;
        for candidate in candidates {
            if best
                .as_ref()
                .is_none_or(|best| (candidate.0, candidate.1) > (best.0, best.1))
            {
                best = Some(candidate);
            }
        }
        Ok(best.map(|(_, _, value)| value))
    }

    async fn aggregate_payload(
        &mut self,
        spec: &DerivedAnalysisSpec,
    ) -> Result<Value, GenericAnalysisError> {
        let id = self.metric_contract(spec.document()).await?;
        let contract_json:String=sqlx::query_scalar(
            "SELECT json_build_object('version',version,'value_kind',value_kind,'unit',unit,'scale_id',scale_id,
             'scale_version',scale_version,'aggregation',aggregation,'allowed_methods',allowed_methods,
             'coverage_policy',coverage_policy,'time_semantics',time_semantics)::text
             FROM metric_definition_versions WHERE id=$1")
            .bind(id).fetch_one(&mut *self.txn).await?;
        let contract = AggregateContract::new(
            id,
            serde_json::from_str(&contract_json).map_err(|_| GenericAnalysisError::Database)?,
            spec,
        )?;
        let window = AggregateWindow::new(spec, &contract)?;
        let source = SourceSelection::from_source(spec.document()["source"].as_str())?;
        self.restore_display_timezone().await?;
        let mut rows = self
            .aggregate_observations(&contract, &window, &source, false)
            .await?;
        let snapshots = if matches!(source.mode, "all" | "measurement") {
            self.aggregate_measurement_snapshot(MeasurementQuery {
                contract: id,
                start: Some(window.measurement_start),
                end: window.end,
                cutoff: window.cutoff,
                source: source.measurement.as_deref(),
                limit: 10001,
                descending: false,
                include_invalid: true,
                retain_evidence: true,
            })
            .await?
        } else {
            Vec::new()
        };
        if snapshots.len() > 10000 {
            return Err(GenericAnalysisError::Value(
                "Metric query exceeds 10000 observations",
            ));
        }
        let identities: HashSet<MeasurementIdentity> =
            snapshots.iter().map(|row| row.identity()).collect();
        let mut input_revisions = Map::new();
        for snapshot in &snapshots {
            if let Some((reference, token)) = &snapshot.evidence {
                input_revisions.insert(reference.clone(), Value::String(token.clone()));
            }
        }
        rows.extend(
            snapshots
                .into_iter()
                .filter(|row| !row.deleted && row.quality == "observed" && row.contract == Some(id))
                .map(|row| row.into_aggregate())
                .collect::<Result<Vec<_>, _>>()?,
        );
        let fallback = self
            .aggregate_measurement_fallback(id, &window, &source, false)
            .await?;
        let mut historical = HashSet::new();
        for supplemental in fallback {
            if supplemental.historical {
                historical.insert(supplemental.identity.clone());
                if !identities.contains(&supplemental.identity) {
                    rows.push(supplemental.row);
                }
            } else if !historical.contains(&supplemental.identity)
                && !identities.contains(&supplemental.identity)
            {
                rows.push(supplemental.row);
            }
        }
        let available: BTreeSet<_> = rows.iter().map(|row| row.source.as_str()).collect();
        let selected = match spec.document()["source"].as_str() {
            Some(source) => Some(source.to_owned()),
            None => {
                if available.len() > 1 {
                    return Err(GenericAnalysisError::Value(
                        "Multiple metric sources; select one source",
                    ));
                }
                available.into_iter().next().map(str::to_owned)
            }
        };
        if let Some(source) = &selected {
            rows.retain(|row| row.source == *source);
        }
        rows.sort_by(|first, second| {
            (
                first.observed,
                first.recorded,
                first.ingested,
                first.sequence,
                first.id.as_str(),
            )
                .cmp(&(
                    second.observed,
                    second.recorded,
                    second.ingested,
                    second.sequence,
                    second.id.as_str(),
                ))
        });
        if contract.value_kind == "increment" {
            rows.retain(|row| {
                let start = row.effective_start.unwrap_or(row.observed);
                window.start <= start && start < window.end
            });
        } else if contract.value_kind == "interval_total" {
            rows.retain(|row| {
                row.effective_end.is_some_and(|end| end <= window.end)
                    && row.effective_start.unwrap_or(row.observed) >= window.start
            });
        }
        if rows.len() > 10000 {
            return Err(GenericAnalysisError::Value(
                "Metric query exceeds 10000 observations",
            ));
        }
        let ends = interval_ends(&rows, contract.gap)?;
        if contract.gap.is_some() {
            rows.retain(|row| {
                ends[&row.id] > window.start
                    && row.effective_start.unwrap_or(row.observed) < window.end
            });
        }
        let predecessor = if contract.method == "delta" && !rows.is_empty() {
            let selected_source = SourceSelection::from_source(selected.as_deref())?;
            self.aggregate_predecessor(
                &contract,
                &window,
                &selected_source,
                source.measurement.as_deref(),
            )
            .await?
        } else {
            None
        };
        let count = rows.len();
        let source_refs: Vec<_> = rows
            .iter()
            .map(|row| {
                row.source_ref
                    .map(|id| id.to_string())
                    .unwrap_or_else(|| "None".to_owned())
            })
            .collect();
        let latest = rows
            .iter()
            .map(|row| row.ingested_display.as_str())
            .max()
            .map(str::to_owned);
        let generation = rows.iter().filter_map(|row| row.projection_version).max();
        let mut revisions = Map::new();
        for row in &rows {
            if let (Some(entry), Some(version)) = (row.source_entry, row.projection_version) {
                revisions.insert(entry.to_string(), Value::from(version));
            }
        }
        revisions.extend(input_revisions);
        let samples: Vec<_> = rows
            .into_iter()
            .map(|row| MetricSample {
                interval_start: row.effective_start.unwrap_or(row.observed),
                interval_end: ends.get(&row.id).copied(),
                value: row.value,
                coverage: row.coverage,
            })
            .collect();
        let aggregate = aggregate_samples(
            &samples,
            &contract.value_kind,
            &contract.method,
            &contract.document["coverage_policy"],
            window.start,
            window.end,
            predecessor.as_ref(),
        )
        .map_err(GenericAnalysisError::Calculation)?;
        drop(samples);
        let cutoff = spec.document()["knowledge_cutoff"]
            .as_str()
            .ok_or(GenericAnalysisError::Database)?;
        let cutoff = cutoff
            .strip_suffix('Z')
            .map_or_else(|| cutoff.to_owned(), |value| format!("{value}+00:00"));
        let mut payload = json!({
            "metric":spec.document()["metric_key"],"source":selected,"metric_version":contract.document["version"],
            "value_kind":contract.value_kind,"method":contract.method,"value":null,
            "unit":contract.document["unit"],"scale_id":contract.document["scale_id"],"scale_version":contract.document["scale_version"],
            "coverage_ratio":aggregate.coverage_ratio,"observations":count,"source_refs":source_refs,
            "input_revisions":null,"projection_generation":generation,"knowledge_cutoff":cutoff,"latest_known_at":latest,
            "spec_hash":spec.hash(),"limitations":["association only; no causal inference","only the selected version, scale, method, and cutoff are comparable"],
        });
        payload["value"] = aggregate.value;
        payload["input_revisions"] = Value::Object(revisions);
        Ok(payload)
    }

    async fn finish_aggregate_document(
        self,
        payload: Value,
    ) -> Result<JsonDocument, GenericAnalysisError> {
        let mut output = JsonSpool::default();
        serde_json::to_writer(&mut output, &payload).map_err(|_| GenericAnalysisError::Database)?;
        drop(payload);
        self.txn.commit().await?;
        output.finish().map_err(|_| GenericAnalysisError::Database)
    }

    pub async fn aggregate_metric(
        mut self,
        spec: &AnalysisSpec,
    ) -> Result<JsonDocument, GenericAnalysisError> {
        let work = async {
            self.prepare(spec).await?;
            let derived = spec
                .derive_aggregate()
                .map_err(|_| GenericAnalysisError::Database)?;
            let payload = self.aggregate_payload(&derived).await?;
            self.finish_aggregate_document(payload).await
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| GenericAnalysisError::Database)?
    }
}
