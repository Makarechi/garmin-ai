use std::collections::BTreeMap;

use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{FromRow, Row, postgres::PgRow};
use time::{Duration, OffsetDateTime, UtcOffset};

use super::{
    ActivityReadError, DailyHealthReader, HealthAnalysisError, MAX_RANGE_DAYS, TemporalPurpose,
    check_calendar_bounds, temporal::temporal_features_in,
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
        Ok(Self {
            id: row.try_get("id")?,
            start: row.try_get("start")?,
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
    at: StoredInstant,
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
        let at = StoredInstant::try_from(self.start).ok()?;
        (seconds >= 1200.0).then_some(KeptRun {
            run: self,
            at,
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
        let (start, end) = (OffsetDateTime::from(start), OffsetDateTime::from(end));
        check_calendar_bounds(start.date(), end.date())
            .map_err(|_| ActivityReadError::CalendarRange)?;
        if end <= start || end - start > Duration::days(MAX_RANGE_DAYS) {
            return Err(ActivityReadError::Range);
        }
        if !(0.0 <= min && min < max && max <= 300.0) {
            return Err(ActivityReadError::HeartRateRange);
        }
        let runs = sqlx::query_as::<_,Run>("SELECT id,start,kind,distance_m,moving_seconds,duration_seconds,avg_hr,ascent_m
            FROM activities WHERE start >= $1 AND start < $2 AND kind IN ('running','trail_running','treadmill_running',
                'track_running','indoor_running','ultra_run','virtual_run','obstacle_run') ORDER BY start LIMIT $3")
            .bind(start).bind(end).bind(MAX_RUNS as i64+1).fetch_all(&mut *self.0).await?;
        let total = runs.len();
        if total > MAX_RUNS {
            return Err(ActivityReadError::ActivityAnalysisLimit);
        }
        let kept = runs
            .into_iter()
            .filter_map(|run| run.with_evidence(min, max))
            .collect::<Vec<_>>();
        let times = kept.iter().map(|run| run.at).collect::<Vec<_>>();
        let cutoff: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *self.0)
            .await?;
        let cutoff = StoredInstant::try_from(cutoff).map_err(|_| HealthAnalysisError::Database)?;
        let contexts = temporal_features_in(
            &mut self.0,
            &METRICS,
            &times,
            cutoff,
            TemporalPurpose::Retrospective,
        )
        .await?;
        self.0.commit().await?;
        let mut groups = BTreeMap::<String, Vec<String>>::new();
        let mut rows = Vec::with_capacity(kept.len());
        for entry in kept {
            let run = entry.run;
            let mut context = contexts
                .get(&entry.at)
                .cloned()
                .ok_or(HealthAnalysisError::Database)?;
            for feature in context.values_mut() {
                for key in [
                    "event_cutoff",
                    "knowledge_cutoff",
                    "observed_at",
                    "ingested_at",
                    "effective_start",
                ] {
                    if let Some(at) = feature[key]
                        .as_str()
                        .and_then(|at| at.parse::<StoredInstant>().ok())
                    {
                        feature[key] = json!(isoformat(at.into()));
                    }
                }
            }
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
        Ok(
            json!({"n":rows.len(),"excluded":total-rows.len(),"rows":rows,"hr_range":[min,max],
                "ordering":"chronological; no cross-condition performance ranking","comparison_groups":groups,
                "mixed_activity_types":mixed,"method_version":"running-summary-comparability-v1",
                "method":"distance / moving duration * 60 / average HR; minimum 20 minutes",
                "limitations":["Chronological descriptive summaries; no global ranking or claim of physiological progress",
                    "Compare similar terrain and activity type; mixed conditions are shown explicitly",
                    "Whole-activity means do not establish steady-state cardiac efficiency",
                    "Pre-event context requires source time; calendar-only HRV is unknown"],
            }),
        )
    }
}

fn isoformat(at: OffsetDateTime) -> String {
    let at = at.to_offset(UtcOffset::UTC);
    let suffix = if at.microsecond() == 0 {
        String::new()
    } else {
        format!(".{:06}", at.microsecond())
    };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}{suffix}+00:00",
        at.year(),
        u8::from(at.month()),
        at.day(),
        at.hour(),
        at.minute(),
        at.second()
    )
}
