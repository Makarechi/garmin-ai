use std::collections::BTreeMap;

use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{FromRow, Postgres, Row, Transaction, postgres::PgRow};
use time::{Duration, OffsetDateTime, UtcOffset};

use super::{
    ActivityReadError, DailyHealthReader, HealthAnalysisError, MAX_RANGE_DAYS, TemporalPurpose,
    check_calendar_bounds, isoformat,
    temporal::{TemporalClock, local_event_clocks, restore_clock_timezone, temporal_features_in},
};

const MAX_RUNS: usize = 2000;
const METRICS: [&str; 3] = ["training_readiness_score", "sleep_score", "hrv_nightly_avg"];
const MISSING: [&str; 5] = [
    "matched_route",
    "weather",
    "sensor_provenance",
    "steady_state_segments",
    "subjective_exertion",
];

struct Run {
    id: String,
    start: OffsetDateTime,
    kind: String,
    distance_m: Option<f64>,
    moving_seconds: Option<f64>,
    duration_seconds: Option<f64>,
    avg_hr: Option<f64>,
    ascent_m: Option<f64>,
}

impl FromRow<'_, PgRow> for Run {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        let start: OffsetDateTime = row.try_get("start")?;
        let offset: i32 = row.try_get("start_offset")?;
        let start = UtcOffset::from_whole_seconds(offset)
            .ok()
            .and_then(|offset| start.checked_to_offset(offset))
            .filter(|start| (1..=9999).contains(&start.year()))
            .ok_or_else(|| {
                sqlx::Error::Decode("Activity time outside the supported calendar".into())
            })?;
        Ok(Self {
            id: row.try_get("id")?,
            start,
            kind: row.try_get("kind")?,
            distance_m: row.try_get("distance_m")?,
            moving_seconds: row.try_get("moving_seconds")?,
            duration_seconds: row.try_get("duration_seconds")?,
            avg_hr: row.try_get("avg_hr")?,
            ascent_m: row.try_get("ascent_m")?,
        })
    }
}

struct KeptRun {
    run: Run,
    distance: f64,
    seconds: f64,
    hr: f64,
}

impl Run {
    fn with_evidence(self, min: f64, max: f64) -> Option<KeptRun> {
        let seconds = self
            .moving_seconds
            .filter(|value| *value != 0.0)
            .or(self.duration_seconds)?;
        let distance = self
            .distance_m
            .filter(|value| *value != 0.0 && value.is_finite())?;
        let hr = self
            .avg_hr
            .filter(|value| *value != 0.0 && (min..=max).contains(value))?;
        (seconds >= 1200.0).then_some(KeptRun {
            run: self,
            distance,
            seconds,
            hr,
        })
    }
}

impl DailyHealthReader {
    /// Describe whole-run averages and attach only pre-run, sourced temporal context.
    pub async fn running_efficiency(
        mut self,
        start: StoredInstant,
        end: StoredInstant,
        min: f64,
        max: f64,
    ) -> Result<Value, ActivityReadError> {
        let result = running_efficiency_in(&mut self.0, start, end, min, max).await?;
        self.0.commit().await?;
        Ok(result)
    }
}

pub(super) async fn running_efficiency_in(
    txn: &mut Transaction<'_, Postgres>,
    start: StoredInstant,
    end: StoredInstant,
    min: f64,
    max: f64,
) -> Result<Value, ActivityReadError> {
    let (start, end) = (OffsetDateTime::from(start), OffsetDateTime::from(end));
    check_calendar_bounds(start.date(), end.date())?;
    if end <= start || end - start > Duration::days(MAX_RANGE_DAYS) {
        return Err(ActivityReadError::Range);
    }
    if !(0.0 <= min && min < max && max <= 300.0) {
        return Err(ActivityReadError::HeartRateRange);
    }
    restore_clock_timezone(txn).await?;
    let runs = sqlx::query_as::<_,Run>("SELECT id,start,extract(timezone FROM start)::integer AS start_offset,kind,distance_m,moving_seconds,duration_seconds,avg_hr,ascent_m
        FROM activities WHERE start >= $1 AND start < $2 AND kind IN ('running','trail_running','treadmill_running',
            'track_running','indoor_running','ultra_run','virtual_run','obstacle_run') ORDER BY start LIMIT $3")
        .bind(start).bind(end).bind(i64::try_from(MAX_RUNS + 1).unwrap_or(i64::MAX)).fetch_all(&mut **txn).await?;
    let total = runs.len();
    if total > MAX_RUNS {
        return Err(ActivityReadError::ActivityAnalysisLimit);
    }
    let times: Vec<_> = runs.iter().map(|run| run.start).collect();
    let cutoff: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let context_clocks = local_event_clocks(&times);
    let contexts = temporal_features_in(
        txn,
        &METRICS,
        &times,
        cutoff,
        TemporalPurpose::Retrospective,
        TemporalClock::DatabaseTimezone,
    )
    .await?;
    let kept = runs
        .into_iter()
        .filter_map(|run| run.with_evidence(min, max))
        .collect::<Vec<_>>();
    let mut groups = BTreeMap::<String, Vec<String>>::new();
    let mut rows = Vec::with_capacity(kept.len());
    for entry in kept {
        let run = entry.run;
        let clock = context_clocks
            .get(&run.start.date().with_time(run.start.time()))
            .map(|(clock, _)| clock)
            .ok_or(HealthAnalysisError::Database)?;
        let context = contexts
            .get(clock)
            .cloned()
            .ok_or(HealthAnalysisError::Database)?;
        rows.push(json!({"activity_id":run.id,"start":isoformat(run.start),"kind":run.kind,
            "distance_m":entry.distance,"avg_hr":entry.hr,"pace_seconds_per_km":entry.seconds/entry.distance*1000.0,
            "meters_per_heartbeat":entry.distance/entry.seconds*60.0/entry.hr,
            "ascent_m_per_km":run.ascent_m.map(|ascent|ascent/entry.distance*1000.0),
            "sleep_score":context.get("sleep_score").and_then(|row|row.get("value")),
            "hrv_nightly_avg":context.get("hrv_nightly_avg").and_then(|row|row.get("value")),
            "readiness":context.get("training_readiness_score").and_then(|row|row.get("value")),
            "context":context,"comparison_group":run.kind,"physiologically_comparable":false,"comparison_missing":MISSING,
        }));
        groups.entry(run.kind).or_default().push(run.id);
    }
    let mixed = groups.len() > 1;
    let groups=groups.into_iter().map(|(kind,ids)|json!({"activity_type":kind,"activity_ids":ids,"n":ids.len(),"status":"descriptive_only"})).collect::<Vec<_>>();
    let mut document = json!({"n":rows.len(),"excluded":total-rows.len(),"hr_range":[min,max],
        "ordering":"chronological; no cross-condition performance ranking","comparison_groups":groups,
        "mixed_activity_types":mixed,"method_version":"running-summary-comparability-v1",
        "method":"distance / moving duration * 60 / average HR; minimum 20 minutes",
        "limitations":["Chronological descriptive summaries; no global ranking or claim of physiological progress",
            "Compare similar terrain and activity type; mixed conditions are shown explicitly",
            "Whole-activity means do not establish steady-state cardiac efficiency",
            "Pre-event context requires source time; calendar-only HRV is unknown"],
    });
    document["rows"] = Value::Array(rows);
    Ok(document)
}
