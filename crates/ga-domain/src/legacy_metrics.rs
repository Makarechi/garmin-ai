use serde_json::{Map, Value, json};

const POINT_INTERVAL: &str = "instantaneous; no fill after last observation";

struct Metric {
    id: &'static str,
    unit: &'static str,
    kind: &'static str,
    minimum: i32,
    maximum: Option<i32>,
    max_gap: i32,
}

const fn metric(
    id: &'static str,
    unit: &'static str,
    kind: &'static str,
    minimum: i32,
    maximum: Option<i32>,
    max_gap: i32,
) -> Metric {
    Metric {
        id,
        unit,
        kind,
        minimum,
        maximum,
        max_gap,
    }
}

const METRICS: [Metric; 8] = [
    metric("heart_rate_bpm", "bpm", "gauge", 1, Some(300), 300),
    metric("stress_score", "score", "gauge", 0, Some(100), 300),
    metric("body_battery", "score", "estimate", 0, Some(100), 900),
    metric("spo2_pct", "%", "gauge", 1, Some(100), 3600),
    metric("respiration_rpm", "rpm", "gauge", 1, Some(100), 300),
    metric("hrv_rmssd_ms", "ms", "gauge", 0, None, 300),
    metric("steps_bucket", "steps", "increment", 0, None, 300),
    metric("hydration_ml", "ml", "daily_summary", 0, None, 300),
];

impl Metric {
    fn public_contract(&self) -> Value {
        let (aggregation, interval) = match self.kind {
            "increment" => (
                "sum",
                "source interval start; increments are not cumulative",
            ),
            "daily_summary" => ("latest", "source calendar date; consumption time unknown"),
            _ => ("time_weighted_mean", POINT_INTERVAL),
        };
        json!({
            "metric_id": self.id, "unit": self.unit, "kind": self.kind,
            "aggregation": aggregation, "minimum": self.minimum, "maximum": self.maximum,
            "max_gap_seconds": self.max_gap, "interval": interval,
            "reset": "not_applicable",
            "quality": "observed only; finite values in canonical units",
            "provenance": "sources are kept separate; never deduplicated by value alone",
        })
    }
}

pub fn legacy_metric_contracts() -> Map<String, Value> {
    METRICS
        .iter()
        .map(|metric| (metric.id.to_owned(), metric.public_contract()))
        .collect()
}
