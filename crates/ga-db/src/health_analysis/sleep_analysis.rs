use std::{collections::BTreeMap, f64::consts::TAU, time::Duration};

use ga_domain::{
    event_input::known_timezone,
    legacy_json::{self, Utf8Layout},
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{DailyHealthReader, HealthAnalysisError, HealthPeriod, owner_timezone};
use crate::Database;

const FIELDS: [&str; 6] = [
    "sleep_seconds",
    "deep_seconds",
    "rem_seconds",
    "light_seconds",
    "awake_seconds",
    "sleep_score",
];
const STAGES: [&str; 3] = ["deep_seconds", "rem_seconds", "light_seconds"];
const LIMITATIONS: [&str; 5] = [
    "Garmin calendar dates identify main summaries; reported naps use their local start date.",
    "No nap report means unknown, not absence. Documented duration is not a complete daily sleep total.",
    "Stages are main-sleep duration summaries, not reconstructed stage intervals. Subjective sleep quality is not Garmin Sleep Score.",
    "Overlapping or incomplete nap intervals are not added to main sleep. Device estimates do not establish recovery or predict migraine.",
    "Nap intervals longer than 24 hours are excluded as invalid input, not interpreted as complete sleep.",
];

#[derive(Clone, Copy, Default, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NapPolicy {
    #[default]
    Separate,
    IncludeConfirmed,
}

impl NapPolicy {
    fn label(self) -> &'static str {
        match self {
            Self::Separate => "separate",
            Self::IncludeConfirmed => "include_confirmed",
        }
    }
}

struct Night {
    start: OffsetDateTime,
    end: OffsetDateTime,
    evidence: Value,
    bedtime: f64,
    wake_time: f64,
}

struct Nap {
    id: Uuid,
    revision: i32,
    timezone: String,
    start: OffsetDateTime,
    end: Option<OffsetDateTime>,
}

impl Nap {
    fn interval(&self) -> Option<(OffsetDateTime, OffsetDateTime)> {
        let end = self.end?;
        let duration = end - self.start;
        (duration.is_positive() && duration <= time::Duration::days(1)).then_some((self.start, end))
    }

    fn document(&self) -> Result<Value, HealthAnalysisError> {
        Ok(json!({
            "event_id":self.id.to_string(), "revision":self.revision, "timezone":self.timezone,
            "start":stamp(self.start)?, "end":self.end.map(stamp).transpose()?,
            "interval_valid":self.interval().is_some(),
        }))
    }
}

impl Database {
    pub async fn sleep_analysis(
        &self,
        start: Date,
        end: Date,
        timezone: &str,
        policy: NapPolicy,
    ) -> Result<Value, HealthAnalysisError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            self.verified_health_reader()
                .await?
                .sleep_analysis(start, end, timezone, policy)
                .await
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }
}

impl DailyHealthReader {
    pub async fn owner_sleep_analysis(
        mut self,
        start: Date,
        end: Date,
        configured_timezone: &str,
        policy: NapPolicy,
    ) -> Result<Value, HealthAnalysisError> {
        let timezone = owner_timezone(&mut self.0, configured_timezone).await?;
        self.sleep_analysis(start, end, &timezone, policy).await
    }

    pub async fn sleep_analysis(
        mut self,
        start: Date,
        end: Date,
        timezone: &str,
        policy: NapPolicy,
    ) -> Result<Value, HealthAnalysisError> {
        let result = sleep_analysis_in(&mut self.0, start, end, timezone, policy).await?;
        self.0.commit().await?;
        Ok(result)
    }
}

pub(super) async fn sleep_analysis_in(
    txn: &mut Transaction<'_, Postgres>,
    start: Date,
    end: Date,
    timezone: &str,
    policy: NapPolicy,
) -> Result<Value, HealthAnalysisError> {
    let period = HealthPeriod::new(start, end)?;
    if period.days() > 31 {
        return Err(HealthAnalysisError::SleepRange);
    }
    if !known_timezone(timezone) {
        return Err(HealthAnalysisError::InvalidTimezone);
    }
    let dates: Vec<Date> = (0..period.days())
        .map(|offset| start + time::Duration::days(offset))
        .collect();
    let summaries = summaries(txn, start, end).await?;
    let nights = nights(txn, &dates, timezone).await?;
    let naps = naps(txn, start, end).await?;
    let overlapping = overlapping_nights(txn, &naps).await?;

    let mut rows = Vec::with_capacity(dates.len());
    let mut durations = Vec::new();
    for day in dates {
        let row = sleep_day(
            day,
            summaries.get(&day),
            nights.get(&format!("sleep:{day}")),
            naps.get(&day).map_or(&[], Vec::as_slice),
            &overlapping,
            policy,
        )?;
        if let Some(duration) = row["documented_sleep_seconds"].as_f64() {
            durations.push(duration);
        }
        rows.push(row);
    }
    let total = rows.len();
    let mut budget = 20_000;
    let mut visible = Vec::new();
    let mut next_day = None;
    for row in rows {
        let input = serde_json::to_vec(&row).map_err(|_| HealthAnalysisError::Database)?;
        let output = legacy_json::encode(&input, Utf8Layout::Archive, 640_000)
            .map_err(|_| HealthAnalysisError::Database)?;
        let size = str::from_utf8(&output)
            .map_err(|_| HealthAnalysisError::Database)?
            .chars()
            .count()
            + 2;
        if size > budget {
            next_day = Some(row["day"].clone());
            break;
        }
        budget -= size;
        visible.push(row);
    }
    let units: Map<String, Value> = FIELDS
        .into_iter()
        .map(|name| {
            (
                name.into(),
                json!(if name == "sleep_score" { "score" } else { "s" }),
            )
        })
        .collect();
    Ok(json!({
        "algorithm_version":"sleep-analysis-v1", "nap_policy":policy.label(),
        "timezone":timezone, "units":units,
        "rows":visible, "rows_truncated":next_day.is_some(), "next_day":next_day,
        "summary":{
            "days":total, "available_sleep_days":durations.len(),
            "mean_documented_sleep_seconds":mean(&durations),
            "bedtime_circular_sd_minutes":regularity(nights.values().map(|night|night.bedtime)),
            "wake_time_circular_sd_minutes":regularity(nights.values().map(|night|night.wake_time)),
            "timed_main_sessions":nights.len(),
        }, "limitations":LIMITATIONS,
    }))
}

async fn summaries(
    txn: &mut Transaction<'_, Postgres>,
    start: Date,
    end: Date,
) -> Result<BTreeMap<Date, Value>, HealthAnalysisError> {
    let rows: Vec<(Date, String)> = sqlx::query_as(
        "SELECT day,row_to_json(health_days)::text FROM health_days WHERE day BETWEEN $1 AND $2",
    )
    .bind(start)
    .bind(end)
    .fetch_all(&mut **txn)
    .await?;
    rows.into_iter()
        .map(|(day, body)| Ok((day, decode(&body)?)))
        .collect()
}

async fn nights(
    txn: &mut Transaction<'_, Postgres>,
    dates: &[Date],
    timezone: &str,
) -> Result<BTreeMap<String, Night>, HealthAnalysisError> {
    let ids = dates
        .iter()
        .map(|day| format!("sleep:{day}"))
        .collect::<Vec<_>>();
    type Row = (String, OffsetDateTime, OffsetDateTime, String, f64, f64);
    let rows: Vec<Row> = sqlx::query_as(
        "SELECT id,start,\"end\",evidence::text,
          floor(extract(epoch FROM (start AT TIME ZONE $2)::time))::float8,
          floor(extract(epoch FROM (\"end\" AT TIME ZONE $2)::time))::float8
         FROM timeline_intervals WHERE id=ANY($1)",
    )
    .bind(ids)
    .bind(timezone)
    .fetch_all(&mut **txn)
    .await?;
    rows.into_iter()
        .map(|(id, start, end, evidence, bedtime, wake_time)| {
            Ok((
                id,
                Night {
                    start,
                    end,
                    evidence: decode(&evidence)?,
                    bedtime,
                    wake_time,
                },
            ))
        })
        .collect()
}

async fn naps(
    txn: &mut Transaction<'_, Postgres>,
    start: Date,
    end: Date,
) -> Result<BTreeMap<Date, Vec<Nap>>, HealthAnalysisError> {
    type Row = (
        Date,
        Uuid,
        i32,
        String,
        OffsetDateTime,
        Option<OffsetDateTime>,
    );
    let rows: Vec<Row> = sqlx::query_as(
        "SELECT (start AT TIME ZONE timezone)::date,id,revision,timezone,start,\"end\"
         FROM events WHERE kind='nap' AND NOT deleted AND status='confirmed' AND source<>'inferred'
          AND start >= (($1::date-1)::timestamp AT TIME ZONE 'UTC')
          AND start < (($2::date+2)::timestamp AT TIME ZONE 'UTC')
          AND (start AT TIME ZONE timezone)::date BETWEEN $1 AND $2
         ORDER BY start,id LIMIT 201",
    )
    .bind(start)
    .bind(end)
    .fetch_all(&mut **txn)
    .await?;
    if rows.len() > 200 {
        return Err(HealthAnalysisError::SleepNapLimit);
    }
    let mut by_day = BTreeMap::<Date, Vec<Nap>>::new();
    for (day, id, revision, timezone, start, end) in rows {
        by_day.entry(day).or_default().push(Nap {
            id,
            revision,
            timezone,
            start,
            end,
        });
    }
    Ok(by_day)
}

async fn overlapping_nights(
    txn: &mut Transaction<'_, Postgres>,
    naps: &BTreeMap<Date, Vec<Nap>>,
) -> Result<Vec<(OffsetDateTime, OffsetDateTime)>, HealthAnalysisError> {
    let windows = naps
        .values()
        .flatten()
        .filter_map(Nap::interval)
        .collect::<Vec<_>>();
    let (Some(first), Some(last)) = (
        windows.iter().map(|(start, _)| *start).min(),
        windows.iter().map(|(_, end)| *end).max(),
    ) else {
        return Ok(Vec::new());
    };
    let rows = sqlx::query_as(
        "SELECT start,\"end\" FROM timeline_intervals
         WHERE starts_with(id,'sleep:') AND start<$2 AND \"end\">$1 LIMIT 1001",
    )
    .bind(first)
    .bind(last)
    .fetch_all(&mut **txn)
    .await?;
    if rows.len() > 1000 {
        return Err(HealthAnalysisError::SleepSessionLimit);
    }
    Ok(rows)
}

fn sleep_day(
    day: Date,
    summary: Option<&Value>,
    night: Option<&Night>,
    naps: &[Nap],
    night_windows: &[(OffsetDateTime, OffsetDateTime)],
    policy: NapPolicy,
) -> Result<Value, HealthAnalysisError> {
    let values: Map<String, Value> = FIELDS
        .into_iter()
        .map(|name| {
            (
                name.into(),
                summary.map_or(Value::Null, |row| row[name].clone()),
            )
        })
        .collect();
    let refs: Map<String, Value> = FIELDS
        .into_iter()
        .map(|name| {
            (
                name.into(),
                summary.map_or(Value::Null, |row| {
                    row["sources"][format!("field:{name}")].clone()
                }),
            )
        })
        .collect();
    let unavailable: Vec<_> = FIELDS
        .into_iter()
        .filter(|name| values[*name].is_null())
        .collect();
    let mut intervals = naps.iter().filter_map(Nap::interval).collect::<Vec<_>>();
    intervals.sort_unstable();
    let complete = intervals.len() == naps.len();
    let overlap = intervals
        .iter()
        .any(|(start, end)| night_windows.iter().any(|(a, b)| start < b && end > a));
    let nap_seconds = merged_seconds(&intervals);
    let recorded = values["sleep_seconds"].as_f64();
    let documented = if policy == NapPolicy::IncludeConfirmed && !naps.is_empty() {
        if night.is_some() && complete && !overlap {
            recorded.zip(nap_seconds).map(|(base, nap)| base + nap)
        } else {
            None
        }
    } else {
        recorded
    };
    let status = if naps.is_empty() {
        "unanswered"
    } else if !complete {
        "incomplete_interval"
    } else if night.is_none() {
        "main_interval_unknown"
    } else if overlap {
        "overlaps_main"
    } else {
        "reported"
    };
    let fractions = recorded
        .filter(|seconds| *seconds > 0.0)
        .and_then(|seconds| {
            let amounts: Vec<_> = STAGES
                .into_iter()
                .filter_map(|name| values[name].as_f64())
                .collect();
            if amounts.len() != 3 || (amounts.iter().sum::<f64>() - seconds).abs() > 60.0 {
                return None;
            }
            Some(
                STAGES
                    .into_iter()
                    .zip(amounts)
                    .map(|(name, value)| (name.to_owned(), json!(value / seconds)))
                    .collect::<Map<_, _>>(),
            )
        });
    let interval = night.map(|night| {
        Ok::<_,HealthAnalysisError>(json!({"start":stamp(night.start)?,"end":stamp(night.end)?,"source_ref":night.evidence["source_ref"]}))
    }).transpose()?;
    let nap_rows = naps
        .iter()
        .take(10)
        .map(Nap::document)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(json!({
        "day":day.to_string(),"values":values,"source_refs":refs,"unavailable":unavailable,
        "main_interval":interval,"naps":nap_rows,"naps_total":naps.len(),"naps_truncated":naps.len()>10,
        "nap_status":status,"reported_nap_seconds":nap_seconds,"documented_sleep_seconds":documented,
        "stage_fractions":fractions,
    }))
}

fn merged_seconds(intervals: &[(OffsetDateTime, OffsetDateTime)]) -> Option<f64> {
    let (&(mut start, mut end), rest) = intervals.split_first()?;
    let mut duration = time::Duration::ZERO;
    for &(next_start, next_end) in rest {
        if next_start <= end {
            end = end.max(next_end);
        } else {
            duration += end - start;
            (start, end) = (next_start, next_end);
        }
    }
    Some((duration + (end - start)).as_seconds_f64())
}

fn decode(body: &str) -> Result<Value, HealthAnalysisError> {
    serde_json::from_str(body).map_err(|_| HealthAnalysisError::Database)
}

fn stamp(value: OffsetDateTime) -> Result<String, HealthAnalysisError> {
    StoredInstant::try_from(value)
        .map(|stamp| stamp.to_string())
        .map_err(|_| HealthAnalysisError::Database)
}

fn mean(series: &[f64]) -> Option<f64> {
    (!series.is_empty()).then(|| series.iter().sum::<f64>() / series.len() as f64)
}

fn regularity(second_of_day: impl Iterator<Item = f64>) -> Option<f64> {
    let mut count = 0;
    let (mut x, mut y) = (0.0, 0.0);
    for seconds in second_of_day {
        let (sin, cos) = (TAU * (seconds / 60.0) / 1440.0).sin_cos();
        x += cos;
        y += sin;
        count += 1;
    }
    if count < 3 {
        return None;
    }
    let strength = (x / f64::from(count)).hypot(y / f64::from(count)).min(1.0);
    (strength > 1e-9).then(|| 1440.0 / TAU * (-2.0 * strength.ln()).sqrt())
}
