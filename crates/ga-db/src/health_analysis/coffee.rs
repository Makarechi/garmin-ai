use std::{collections::BTreeMap, time::Duration};

use ga_domain::{
    legacy_json::{Utf8Layout, encode, encode_ascii, sum_numbers},
    sha256_hex,
    statistics::{block_mean_difference, describe},
};
use serde_json::{Value, json};
use sqlx::{FromRow, Postgres, QueryBuilder, Row, postgres::PgRow};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{DailyHealthReader, HealthAnalysisError, HealthPeriod, isoformat};
use crate::diary::caffeine_total;

struct Night {
    start: OffsetDateTime,
    end: OffsetDateTime,
    source: Value,
}

struct DiaryInput {
    id: Uuid,
    start: OffsetDateTime,
    end: Option<OffsetDateTime>,
    kind: String,
    status: String,
    revision: i32,
    payload: Value,
}

impl FromRow<'_, PgRow> for DiaryInput {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        let payload: String = row.try_get("payload")?;
        Ok(Self {
            id: row.try_get("id")?,
            start: row.try_get("start")?,
            end: row.try_get("end")?,
            kind: row.try_get("kind")?,
            status: row.try_get("status")?,
            revision: row.try_get("revision")?,
            payload: serde_json::from_str(&payload)
                .map_err(|error| sqlx::Error::Decode(Box::new(error)))?,
        })
    }
}

impl DailyHealthReader {
    /// Compare measured main sleep using explicitly documented caffeine coverage.
    pub async fn coffee_sleep(
        mut self,
        start: Date,
        end: Date,
        late_hours: Option<f64>,
        outcome: &str,
    ) -> Result<Value, HealthAnalysisError> {
        let period = HealthPeriod::new(start, end)?;
        let hours = late_hours.unwrap_or(6.0);
        if period.days() > 31
            || !hours.is_finite()
            || !(0.0 < hours && hours <= 24.0)
            || !matches!(outcome, "sleep_score" | "sleep_seconds")
        {
            return Err(HealthAnalysisError::DateRange);
        }
        let work = async {
            let days: Vec<_> = (0..period.days())
                .map(|offset| start + time::Duration::days(offset))
                .collect();
            let ids: Vec<_> = days.iter().map(|day| format!("sleep:{day}")).collect();
            let stored: Vec<(String,OffsetDateTime,OffsetDateTime,String)> = sqlx::query_as(
                "SELECT id,start,\"end\",coalesce((evidence->'source_ref')::text,'null') FROM timeline_intervals WHERE id=ANY($1)",
            ).bind(&ids).fetch_all(&mut *self.0).await?;
            let mut nights = BTreeMap::new();
            for (id, start, end, source) in stored {
                nights.insert(
                    id,
                    Night {
                        start,
                        end,
                        source: json_value(&source)?,
                    },
                );
            }
            let mut summary_sql = QueryBuilder::<Postgres>::new("SELECT day,");
            summary_sql
                .push(outcome)
                .push("::double precision,coalesce((sources->")
                .push_bind(format!("field:{outcome}"))
                .push(")::text,'null') FROM health_days WHERE day BETWEEN ")
                .push_bind(start)
                .push(" AND ")
                .push_bind(end);
            let summaries: Vec<(Date, Option<f64>, String)> =
                summary_sql.build_query_as().fetch_all(&mut *self.0).await?;
            let summaries: BTreeMap<_, _> = summaries
                .into_iter()
                .map(|(day, value, source)| {
                    json_value(&source).map(|source| (day, (value, source)))
                })
                .collect::<Result<_, _>>()?;
            let mut events = Vec::new();
            if let (Some(left), Some(right)) = (
                nights
                    .values()
                    .map(|night| night.start - time::Duration::hours(24))
                    .min(),
                nights.values().map(|night| night.end).max(),
            ) {
                events = sqlx::query_as(include_str!("coffee_inputs.sql"))
                    .bind(left)
                    .bind(right)
                    .fetch_all(&mut *self.0)
                    .await?;
                if events.len() > 1000 {
                    return Err(HealthAnalysisError::CoffeeInputLimit);
                }
            }
            self.0.commit().await?;
            let mut rows = Vec::with_capacity(days.len());
            let mut late = Vec::new();
            let mut not_late = Vec::new();
            for day in days {
                let summary = summaries.get(&day);
                let value = summary.and_then(|(value, _)| *value);
                let mut row = json!({"day":day.to_string(),"outcome":value,"eligible":false,"exclusions":[],"inputs":[]});
                match nights
                    .get(&format!("sleep:{day}"))
                    .filter(|night| night.end > night.start)
                {
                    None => row["exclusions"] = json!(["missing_sleep_interval"]),
                    Some(night) => {
                        let cohort = populate_row(&mut row, night, summary, &events, hours)?;
                        if let (Some(cohort), Some(value)) = (cohort, value) {
                            if cohort {
                                late.push(value);
                            } else {
                                not_late.push(value);
                            }
                        }
                    }
                }
                rows.push(row);
                // A partial row array already over budget cannot shrink in the final envelope.
                if encoded_length(&json!(rows))? > 40000 {
                    return Err(HealthAnalysisError::CoffeeEvidenceLimit);
                }
            }
            let spec = json!({
                "outcome":outcome,"exposure":"recorded_caffeine_in_late_window",
                "cohort_definitions":{
                    "late":"At least one caffeine event in [sleep_start - late_hours, sleep_start)",
                    "not_late":"No caffeine event in the late window; includes earlier caffeine and none in the complete 24-hour diary",
                },
                "late_hours":late_hours.map_or(json!(6),|hours| json!(hours)),
                "lookback_hours":24,"start":start.to_string(),"end":end.to_string(),
                "unit_of_observation":"main_sleep_session","minimum_per_cohort":5,
                "method_version":"coffee-sleep-v1",
                "exclusions":["missing_interval_or_outcome","inconsistent_sleep_source",
                    "unconfirmed_caffeine_or_confounder","incomplete_diary","contradictory_absence","recorded_illness_or_travel"],
            });
            let sufficient = late.len().min(not_late.len()) >= 5;
            let comparison = sufficient.then(|| block_mean_difference(&late,&not_late)).map(|estimate| json!({
                "difference":estimate.difference,"ci95":estimate.ci95,"method":estimate.method,
            }));
            let eligible = rows.iter().filter(|row| row["eligible"] == true).count();
            let input = serde_json::to_vec(&json!({"spec":spec,"rows":rows}))
                .map_err(|_| HealthAnalysisError::Database)?;
            let fingerprint = encode_ascii(&input, Utf8Layout::Archive, input.len())
                .map_err(|_| HealthAnalysisError::Database)?;
            let result = json!({
                "spec":spec,"status":if sufficient {"exploratory"} else {"insufficient_evidence"},
                "rows":rows,"eligible":eligible,"excluded":period.days()-eligible as i64,
                "cohorts":{"late":description(&late),"not_late":description(&not_late)},
                "comparison":comparison,
                "limitations":[
                    "Observational, not causal or a recommendation to change dose",
                    "No diary record is not zero caffeine; explicit full-window coverage is required",
                    "Unrecorded confounders and self-report errors remain possible",
                    "Main sleep only; calendar gaps and repeated exploratory checks limit inference",
                    "No confirmed replication or multiplicity adjustment",
                ],
                "next_data_needed":if sufficient {None} else {Some("Confirm completeness of caffeine records for the pre-sleep window; both timing cohorts need enough nights")},
                "evidence_hash":sha256_hex(fingerprint.as_bytes()),
            });
            if encoded_length(&result)? > 40000 {
                return Err(HealthAnalysisError::CoffeeEvidenceLimit);
            }
            Ok(result)
        };
        tokio::time::timeout(Duration::from_millis(1800), work)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
}

fn populate_row(
    row: &mut Value,
    night: &Night,
    summary: Option<&(Option<f64>, Value)>,
    events: &[DiaryInput],
    hours: f64,
) -> Result<Option<bool>, HealthAnalysisError> {
    let left = night.start - time::Duration::hours(24);
    let right = night.start;
    let relevant: Vec<_> = events
        .iter()
        .filter(|event| {
            event.start
                < if matches!(event.kind.as_str(), "illness" | "travel") {
                    night.end
                } else {
                    right
                }
                && match event.end {
                    Some(end) if end == event.start => event.start >= left,
                    Some(end) => end > left,
                    None => event.kind == "illness" || event.start >= left,
                }
        })
        .collect();
    let uncertain: Vec<_> = relevant
        .iter()
        .copied()
        .filter(|event| {
            event.status != "confirmed"
                && matches!(event.kind.as_str(), "caffeine" | "illness" | "travel")
        })
        .collect();
    let confirmed: Vec<_> = relevant
        .iter()
        .copied()
        .filter(|event| event.status == "confirmed")
        .collect();
    let coffee: Vec<_> = confirmed
        .iter()
        .copied()
        .filter(|event| event.kind == "caffeine" && left <= event.start && event.start < right)
        .collect();
    let absence: Vec<_> = confirmed
        .iter()
        .copied()
        .filter(|event| event.kind == "caffeine_absence" && event.end.is_some())
        .collect();
    let mut coverage: Vec<_> = confirmed
        .iter()
        .filter(|event| {
            matches!(
                event.kind.as_str(),
                "caffeine_absence" | "caffeine_log_complete"
            )
        })
        .filter_map(|event| event.end.map(|end| (event.start, end)))
        .collect();
    coverage.sort_unstable();
    let mut covered_until = left;
    for (start, end) in coverage {
        if end <= covered_until {
            continue;
        }
        if start > covered_until {
            break;
        }
        covered_until = covered_until.max(end);
        if covered_until >= right {
            break;
        }
    }
    let covered = covered_until >= right;
    let conflict = absence.iter().any(|absence| {
        coffee.iter().any(|coffee| {
            absence.start <= coffee.start && absence.end.is_some_and(|end| coffee.start < end)
        })
    });
    let mut inputs = Vec::with_capacity(relevant.len());
    for event in relevant {
        let mut input = json!({"event_id":event.id.to_string(),"revision":event.revision,
            "kind":event.kind,"status":event.status,"start":isoformat(event.start),"end":event.end.map(isoformat)});
        if event.kind == "caffeine" {
            input["dose"] =
                caffeine_total(&event.payload).map_err(|_| HealthAnalysisError::Database)?;
        }
        inputs.push(input);
    }
    row["sleep_interval"] = json!({"start":isoformat(night.start),"end":isoformat(night.end),"source_ref":night.source});
    let source = summary.map_or(Value::Null, |(_, source)| source.clone());
    row["outcome_source_ref"] = source.clone();
    row["exposure_start"] = json!(isoformat(left));
    row["exposure_end"] = json!(isoformat(right));
    row["diary_complete"] = json!(covered);
    row["coverage_conflict"] = json!(conflict);
    row["last_recorded_caffeine_hours_before_sleep"] = json!(
        coffee
            .iter()
            .map(|event| event.start)
            .max()
            .map(|at| (right - at).whole_microseconds() as f64 / 1_000_000.0 / 3600.0)
    );
    row["inputs"] = json!(inputs);
    let mut exclusions = Vec::new();
    if !uncertain.is_empty() {
        exclusions.push("unconfirmed_caffeine_or_confounder");
    }
    if !covered {
        exclusions.push("incomplete_caffeine_diary");
    }
    if conflict {
        exclusions.push("contradictory_absence");
    }
    if row["outcome"].is_null() {
        exclusions.push("missing_outcome");
    }
    if !truthy(&source) || source != night.source {
        exclusions.push("inconsistent_sleep_source");
    }
    if confirmed
        .iter()
        .any(|event| matches!(event.kind.as_str(), "illness" | "travel"))
    {
        exclusions.push("recorded_illness_or_travel");
    }
    let doses: Vec<_> = coffee
        .iter()
        .map(|event| caffeine_total(&event.payload).map_err(|_| HealthAnalysisError::Database))
        .collect::<Result<_, _>>()?;
    let mut totals = serde_json::Map::new();
    for field in ["min", "estimate", "max"] {
        let value = if covered
            && !conflict
            && !uncertain.iter().any(|event| event.kind == "caffeine")
            && doses.iter().all(|dose| !dose[field].is_null())
        {
            sum_numbers(doses.iter().map(|dose| &dose[field]))
                .map_err(|_| HealthAnalysisError::Database)?
        } else {
            Value::Null
        };
        totals.insert(field.to_owned(), value);
    }
    row["total_caffeine_mg"] = Value::Object(totals);
    row["exclusions"] = json!(exclusions);
    if exclusions.is_empty() {
        let duration = late_window(hours);
        let late = coffee.iter().any(|event| event.start >= right - duration);
        row["eligible"] = json!(true);
        row["cohort"] = json!(if late { "late" } else { "not_late" });
        Ok(Some(late))
    } else {
        Ok(None)
    }
}

fn json_value(text: &str) -> Result<Value, HealthAnalysisError> {
    serde_json::from_str(text).map_err(|_| HealthAnalysisError::Database)
}

fn late_window(hours: f64) -> time::Duration {
    let fractional = hours.fract() * 3_600_000_000.0;
    let mut micros = hours.trunc() as i64 * 3_600_000_000 + fractional.trunc() as i64;
    if fractional.fract() > 0.5 || (fractional.fract() == 0.5 && micros % 2 != 0) {
        micros += 1;
    }
    time::Duration::microseconds(micros)
}

fn encoded_length(value: &Value) -> Result<usize, HealthAnalysisError> {
    let bytes = serde_json::to_vec(value).map_err(|_| HealthAnalysisError::Database)?;
    encode(&bytes, Utf8Layout::Ordered, bytes.len())
        .map(|bytes| bytes.len())
        .map_err(|_| HealthAnalysisError::Database)
}

fn description(values: &[f64]) -> Value {
    let stats = describe(values.iter().copied().map(Some));
    json!({"n":stats.n,"mean":stats.mean,"median":stats.median,"sd":stats.sd,"min":stats.min,"max":stats.max})
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::late_window;

    #[test]
    fn keeps_python_hour_rounding_at_microsecond_boundaries() -> Result<(), serde_json::Error> {
        let cases: Vec<(f64, i64)> =
            serde_json::from_str(include_str!("../../tests/fixtures/coffee-hours.json"))?;
        for (hours, expected) in cases {
            assert_eq!(
                late_window(hours).whole_microseconds(),
                i128::from(expected),
                "hours {hours}"
            );
        }
        Ok(())
    }
}
