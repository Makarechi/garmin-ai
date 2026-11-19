use ga_domain::access::{ReadToolInput, Scopes};
use serde_json::{Value, json};

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum ReadTool {
    Snapshot,
    Range,
    Activities,
    ActivityDetails,
    Events,
    EventDefinitions,
    ResourceCatalog,
    GenericAnalysis,
    Timeline,
    DeviceHistory,
    Freshness,
    Baseline,
    ComparePeriods,
    RunningEfficiency,
    MetricSeries,
    StressHours,
    LaggedAssociation,
    EventWindows,
    MigraineWindows,
    WellbeingObservations,
    CoffeeSleep,
    Insights,
    Sleep,
}

impl ReadTool {
    pub const GARMIN_READS: [Self; 17] = [
        Self::Snapshot,
        Self::Range,
        Self::Activities,
        Self::ActivityDetails,
        Self::DeviceHistory,
        Self::Baseline,
        Self::ComparePeriods,
        Self::RunningEfficiency,
        Self::MetricSeries,
        Self::StressHours,
        Self::LaggedAssociation,
        Self::EventWindows,
        Self::MigraineWindows,
        Self::CoffeeSleep,
        Self::Insights,
        Self::Sleep,
        Self::Timeline,
    ];
    pub const IMPLEMENTED: [Self; 23] = [
        Self::Snapshot,
        Self::Range,
        Self::MetricSeries,
        Self::StressHours,
        Self::Activities,
        Self::ActivityDetails,
        Self::Events,
        Self::EventDefinitions,
        Self::ResourceCatalog,
        Self::GenericAnalysis,
        Self::Timeline,
        Self::Freshness,
        Self::Insights,
        Self::Baseline,
        Self::ComparePeriods,
        Self::RunningEfficiency,
        Self::EventWindows,
        Self::MigraineWindows,
        Self::LaggedAssociation,
        Self::Sleep,
        Self::WellbeingObservations,
        Self::CoffeeSleep,
        Self::DeviceHistory,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Self::Snapshot => "health_snapshot",
            Self::Range => "health_range",
            Self::Activities => "activities",
            Self::ActivityDetails => "activity_details",
            Self::Events => "events",
            Self::EventDefinitions => "event_definitions",
            Self::ResourceCatalog => "resource_catalog",
            Self::GenericAnalysis => "generic_analysis",
            Self::Timeline => "timeline",
            Self::DeviceHistory => "device_history",
            Self::Freshness => "data_freshness",
            Self::Baseline => "personal_baseline",
            Self::ComparePeriods => "analysis_compare_periods",
            Self::RunningEfficiency => "analysis_running_efficiency",
            Self::MetricSeries => "metric_series",
            Self::StressHours => "analysis_stress_by_hour",
            Self::LaggedAssociation => "analysis_lagged_association",
            Self::EventWindows => "analysis_event_windows",
            Self::MigraineWindows => "analysis_migraine_windows",
            Self::WellbeingObservations => "wellbeing_observations",
            Self::CoffeeSleep => "analysis_coffee_sleep",
            Self::Insights => "insights_list",
            Self::Sleep => "analysis_sleep",
        }
    }

    pub fn catalog_entry(self) -> Value {
        let date = |title| json!({"format": "date", "title": title, "type": "string"});
        let datetime = |title| json!({"format": "date-time", "title": title, "type": "string"});
        let typed_range = || {
            json!({
                "start": datetime("Start"),
                "end": datetime("End"),
                "kind": {"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Kind"}
            })
        };
        let (description, title, properties, required) = match self {
            Self::Snapshot => (
                "Daily Garmin values and explicit missing metrics for one local date.",
                "health_snapshotArgs",
                json!({"day": date("Day")}),
                &["day"][..],
            ),
            Self::Range => (
                "Stored daily summaries for an inclusive date range.",
                "health_rangeArgs",
                json!({"start": date("Start"), "end": date("End")}),
                &["start", "end"][..],
            ),
            Self::Activities => (
                "List stored activity summaries in a half-open timestamp range.",
                "activitiesArgs",
                typed_range(),
                &["start", "end"][..],
            ),
            Self::ActivityDetails => (
                "Details, laps and HR zones for one activity. Raw samples are excluded by default. Use next_offset to continue and a smaller limit for bulky samples.",
                "activity_detailsArgs",
                json!({
                    "activity_id":{"title":"Activity Id","type":"string"},
                    "include_samples":{"default":false,"title":"Include Samples","type":"boolean"},
                    "offset":{"default":0,"title":"Offset","type":"integer"},
                    "limit":{"default":100,"title":"Limit","type":"integer"},
                }),
                &["activity_id"][..],
            ),
            Self::Events => (
                "Read confirmed, inferred, or pending diary events with their status; excludes deleted records.",
                "eventsArgs",
                typed_range(),
                &["start", "end"][..],
            ),
            Self::Timeline => (
                "Overlapping sleep, activity, wellbeing, context and plan layers with evidence. Segment labels are a legacy display projection; annotations preserve overlaps. Points cover no duration; calendar plans do not prove attendance. At most 500 annotations in 31 days.",
                "timelineArgs",
                json!({"start": datetime("Start"), "end": datetime("End")}),
                &["start", "end"][..],
            ),
            Self::DeviceHistory => (
                "Bounded activity-scoped FIT device and historical zone evidence; no serial numbers or inferred sensor attribution.",
                "device_historyArgs",
                json!({
                    "start": datetime("Start"), "end": datetime("End"),
                    "limit": {"default":100,"title":"Limit","type":"integer"},
                }),
                &["start", "end"][..],
            ),
            Self::Baseline => (
                "Compute a personal baseline from available daily values with sample size and missing days.",
                "personal_baselineArgs",
                json!({"metric":{"type":"string","title":"Metric"},"start":date("Start"),"end":date("End")}),
                &["metric", "start", "end"][..],
            ),
            Self::ComparePeriods => (
                "Compare non-overlapping daily periods with block-bootstrap uncertainty when enough observations exist.",
                "analysis_compare_periodsArgs",
                json!({"metric":{"type":"string","title":"Metric"},"a_start":date("A Start"),"a_end":date("A End"),
                       "b_start":date("B Start"),"b_end":date("B End")}),
                &["metric", "a_start", "a_end", "b_start", "b_end"][..],
            ),
            Self::RunningEfficiency => (
                "Describe runs chronologically within the requested HR range, separating activity types; missing route/weather/sensor/RPE evidence prevents physiological ranking.",
                "analysis_running_efficiencyArgs",
                json!({"start":datetime("Start"),"end":datetime("End"),
                       "hr_min":{"type":"number","title":"Hr Min","default":0},
                       "hr_max":{"type":"number","title":"Hr Max","default":250}}),
                &["start", "end"][..],
            ),
            Self::MetricSeries => (
                "Intraday gauges use coverage-gated time-weighted means; steps_bucket uses sum. Returns metric contract and source provenance. Hydration is a daily summary in health_snapshot, not an intraday drink event. Range is half-open.",
                "metric_seriesArgs",
                json!({"metric":{"type":"string","title":"Metric"},"start":datetime("Start"),
                    "end":datetime("End"),"minutes":{"type":"integer","title":"Minutes","default":5}}),
                &["metric", "start", "end"][..],
            ),
            Self::Sleep => (
                "Main sleep timestamps in rows[].main_interval, stages and naps for at most 31 days. Dates select Garmin's wake-day summary: today's sleep may start yesterday evening. timing_only=true requires start=end and nap_policy=separate; it returns exactly one verified main sleep interval without waiting for unrelated history replay. For a multi-day pattern or summary use timing_only=false. Missing nap logs are unknown; no recovery or prediction claims.",
                "analysis_sleepArgs",
                json!({
                    "start":date("Start"),"end":date("End"),
                    "nap_policy":{"type":"string","enum":["separate","include_confirmed"],"title":"Nap Policy","default":"separate"},
                    "timing_only":{"type":"boolean","title":"Timing Only","default":false}
                }),
                &["start", "end"][..],
            ),
            Self::LaggedAssociation => (
                "Exploratory daily correlations at specified calendar-day lags; association is not causation.",
                "analysis_lagged_associationArgs",
                json!({"metric_a":{"title":"Metric A","type":"string"},
                    "metric_b":{"title":"Metric B","type":"string"},
                    "start":date("Start"),"end":date("End"),
                    "lags":{"title":"Lags","type":"array","items":{"type":"integer"}}}),
                &["metric_a", "metric_b", "start", "end", "lags"][..],
            ),
            Self::EventWindows => (
                "Compute measured physiology in -48h through +24h windows around diary events.",
                "analysis_event_windowsArgs",
                json!({"event_type":{"title":"Event Type","type":"string"},
                    "metric":{"title":"Metric","type":"string"},
                    "start":datetime("Start"),"end":datetime("End")}),
                &["event_type", "metric", "start", "end"][..],
            ),
            Self::StressHours => (
                "Rank local clock hours by the share of observed Garmin stress time at score 51 or higher, with coverage and missing-hour limits. At most 31 days inclusive; diary entries are not required.",
                "analysis_stress_by_hourArgs",
                json!({"start":date("Start"),"end":date("End"),
                    "timezone":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Timezone"}}),
                &["start", "end"][..],
            ),
            Self::MigraineWindows => (
                "Match logged migraine days to weekday controls with explicit limitations and sample sizes.",
                "analysis_migraine_windowsArgs",
                json!({"metric":{"title":"Metric","type":"string"},
                    "start":date("Start"),"end":date("End"),
                    "timezone":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Timezone"}}),
                &["metric", "start", "end"][..],
            ),
            Self::WellbeingObservations => (
                "Reported energy, restedness, pain and daily-function impact, independent of Garmin scores. At most 31 days and 200 reports per bounded page; pass next_cursor as cursor with the same range to continue. Missing values remain unknown.",
                "wellbeing_observationsArgs",
                json!({"start":datetime("Start"),"end":datetime("End"),
                    "cursor":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Cursor"}}),
                &["start", "end"][..],
            ),
            Self::CoffeeSleep => (
                "Compare caffeine timing and main sleep over at most 31 inclusive calendar dates using explicit diary coverage, exclusions and versioned evidence; missing diary is never zero intake.",
                "analysis_coffee_sleepArgs",
                json!({"start":date("Start"),"end":date("End"),
                    "late_hours":{"default":6,"title":"Late Hours","type":"number"},
                    "outcome":{"default":"sleep_score","enum":["sleep_score","sleep_seconds"],"title":"Outcome","type":"string"}}),
                &["start", "end"][..],
            ),
            Self::Insights => (
                "Stored insights, evidence and candidate/accepted status.",
                "insights_listArgs",
                json!({"limit":{"default":30,"title":"Limit","type":"integer"},
                    "cursor":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Cursor"}}),
                &[][..],
            ),
            Self::EventDefinitions => (
                "Page through system, custom and retired definitions and immutable version contracts.",
                "event_definitionsArgs",
                json!({
                    "after_key":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"After Key"},
                    "definition_key":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Definition Key"},
                    "before_version":{"anyOf":[{"type":"integer"},{"type":"null"}],"default":null,"title":"Before Version"},
                    "limit":{"default":10,"title":"Limit","type":"integer"}
                }),
                &[][..],
            ),
            Self::ResourceCatalog => (
                "Discover permitted versioned resources; request details by key and page with next_cursor.",
                "resource_catalogArgs",
                json!({
                    "kind":{"enum":["events","system_metrics","tracker_metrics","raw_sources"],"title":"Kind","type":"string"},
                    "after_key":{"anyOf":[{"type":"string","maxLength":200},{"type":"null"}],"default":null,"title":"After Key"},
                    "resource_key":{"anyOf":[{"type":"string","maxLength":200},{"type":"null"}],"default":null,"title":"Resource Key"},
                    "limit":{"default":10,"title":"Limit","type":"integer"},
                    "detail":{"default":false,"title":"Detail","type":"boolean"}
                }),
                &["kind"][..],
            ),
            Self::Freshness => (
                "Separate API fetch times, observed-signal lag, coverage and current-state quality gates.",
                "data_freshnessArgs",
                json!({}),
                &[][..],
            ),
            Self::GenericAnalysis => {
                return serde_json::from_str(include_str!("../contracts/generic-analysis.json"))
                    .unwrap_or(Value::Null);
            }
        };
        let mut input_schema = json!({
            "title": title, "type": "object", "additionalProperties": false,
            "properties": properties,
        });
        if !required.is_empty() {
            input_schema["required"] = json!(required);
        }
        json!({
            "name": self.name(),
            "description": description,
            "input_schema": input_schema,
        })
    }

    pub fn permits(self, scopes: Scopes) -> bool {
        scopes.permits_tool(self.name(), ReadToolInput::Discovery)
    }
}
