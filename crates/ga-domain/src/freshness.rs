use serde_json::Value;
use time::OffsetDateTime;

use crate::{
    event_input::source_offset_datetime, localization::catalog_text, timestamp::StoredInstant,
};

const METRICS: [&str; 8] = [
    "heart_rate_bpm",
    "stress_score",
    "body_battery",
    "respiration_rpm",
    "spo2_pct",
    "sleep_score",
    "hrv_nightly_avg",
    "training_readiness_score",
];

/// Render the application-verified age and coverage of the permitted channels.
pub fn render_current_state_freshness(
    channels: &Value,
    now: StoredInstant,
    timezone: &str,
    locale: &str,
) -> Option<String> {
    let now = source_offset_datetime(now, timezone)?;
    let message = |key: &str| catalog_text(locale, key).unwrap_or_default();
    let mut lines = Vec::new();
    let mut frequent = 0;
    let mut recently_checked = true;
    for metric in METRICS {
        let channel = &channels[metric];
        if !channel.as_object().is_some_and(|fields| !fields.is_empty()) {
            continue;
        }
        let label = message(&format!("freshness.metric.{metric}"));
        let quality = channel["quality_reason"].as_str().unwrap_or("unknown");
        let ordinary_reason = catalog_text(locale, &format!("freshness.reason.{quality}"))
            .unwrap_or_else(|| message("freshness.reason.unknown"));
        let (detail, reason) = if channel["semantics"] == "daily_summary" {
            let day = channel["source_calendar_date"]
                .as_str()
                .filter(|value| !value.is_empty());
            let detail = day.map_or_else(
                || message("freshness.summary_missing").to_owned(),
                |day| message("freshness.summary_on").replace("{date}", day),
            );
            let reason = if channel["usable_as_daily_summary"] == true {
                message("freshness.summary_usable")
            } else if day.is_some() {
                message("freshness.summary_stale")
            } else {
                ordinary_reason
            };
            (detail, reason.to_owned())
        } else {
            let observed = channel["newest_observed_at"]
                .as_str()
                .filter(|value| !value.is_empty())
                .map(|value| observation_time(value, now, timezone))
                .transpose()
                .ok()?;
            let lag = channel["observation_lag_seconds"].as_f64();
            let detail = match (observed, lag) {
                (Some(observed), Some(lag)) => message("freshness.observed_with_lag")
                    .replace("{time}", &observed)
                    .replace("{lag}", &lag_text(lag, locale)),
                (Some(observed), None) => {
                    message("freshness.observed").replace("{time}", &observed)
                }
                (None, _) => message("freshness.no_measurements").to_owned(),
            };
            let reason = if channel["refresh_mode"] == "daily" {
                message("freshness.daily").replace("{reason}", ordinary_reason)
            } else {
                ordinary_reason.to_owned()
            };
            (detail, reason)
        };
        lines.push(
            message("freshness.line")
                .replace("{label}", label)
                .replace("{detail}", &detail)
                .replace("{reason}", &reason),
        );
        if channel["refresh_mode"] == "frequent" {
            frequent += 1;
            recently_checked &= channel["usable_for_current_state"] != true
                && matches!(quality, "stale_observation" | "source_empty")
                && channel["fetch_status"]
                    .as_str()
                    .is_some_and(|status| !matches!(status, "error" | "fetch_error"))
                && channel["fetch_lag_seconds"]
                    .as_f64()
                    .is_some_and(|lag| lag <= 1800.0);
        }
    }
    if lines.is_empty() {
        return Some(String::new());
    }
    let heading = if frequent > 0 && recently_checked {
        format!(
            "{}\n{}",
            message("freshness.heading"),
            message("freshness.checked_recently")
        )
    } else {
        message("freshness.heading").to_owned()
    };
    Some(format!("{heading}\n{}", lines.join("\n")))
}

fn observation_time(value: &str, now: OffsetDateTime, timezone: &str) -> Result<String, ()> {
    let at = StoredInstant::from_python_isoformat(value)
        .and_then(|at| source_offset_datetime(at, timezone))
        .ok_or(())?;
    let clock = format!("{:02}:{:02}", at.hour(), at.minute());
    if at.date() == now.date() {
        Ok(clock)
    } else if at.year() == now.year() {
        Ok(format!(
            "{:02}.{:02} {clock}",
            at.day(),
            u8::from(at.month())
        ))
    } else {
        Ok(format!(
            "{:02}.{:02}.{:04} {clock}",
            at.day(),
            u8::from(at.month()),
            at.year()
        ))
    }
}

fn lag_text(seconds: f64, locale: &str) -> String {
    let minutes = (seconds.max(0.0) / 60.0).floor() as u64;
    if minutes == 0 {
        return catalog_text(locale, "freshness.lag.under_minute")
            .unwrap_or_default()
            .to_owned();
    }
    let days = minutes / 1440;
    let hours = (minutes % 1440) / 60;
    let minutes = if days == 0 { minutes % 60 } else { 0 };
    [
        (days, "freshness.lag.days"),
        (hours, "freshness.lag.hours"),
        (minutes, "freshness.lag.minutes"),
    ]
    .into_iter()
    .filter(|(count, _)| *count != 0)
    .map(|(count, key)| {
        catalog_text(locale, key)
            .unwrap_or_default()
            .replace("{count}", &count.to_string())
    })
    .collect::<Vec<_>>()
    .join(" ")
}

#[cfg(test)]
mod tests {
    use std::error::Error;

    use serde_json::json;

    use super::render_current_state_freshness;

    #[test]
    fn renders_saved_observations_and_daily_summaries_separately() -> Result<(), Box<dyn Error>> {
        let channels = json!({
            "heart_rate_bpm": {
                "newest_observed_at": "2026-01-01T23:00:00+00:00",
                "observation_lag_seconds": 5400,
                "quality_reason": "stale_observation",
                "refresh_mode": "frequent",
                "usable_for_current_state": false,
                "fetch_status": "normalized",
                "fetch_lag_seconds": 90,
            },
            "sleep_score": {
                "semantics": "daily_summary",
                "source_calendar_date": "2025-12-31",
                "usable_as_daily_summary": false,
            },
        });
        let at = "2026-01-02T00:30:00+00:00".parse()?;
        let rendered = render_current_state_freshness(&channels, at, "UTC", "en");
        assert_eq!(
            rendered.as_deref(),
            Some(concat!(
                "Data recency at the time of your question:\n",
                "Garmin was checked recently but returned no newer measurements.\n",
                "— Heart rate: last reading at 01.01 23:00 (1h 30min ago); no new measurements yet.\n",
                "— Sleep: summary for 2025-12-31; the daily summary is outdated or its recency is unconfirmed."
            ))
        );
        Ok(())
    }
}
