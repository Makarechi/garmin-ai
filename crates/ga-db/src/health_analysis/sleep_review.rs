use ga_domain::{
    event_input::source_offset_datetime, garmin_summary::gmt_instant, statistics::describe,
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{HealthAnalysisError, HealthPeriod, sleep_analysis::reported_naps};
use crate::activity_documents::PARSER_VERSION;

const METRICS: [&str; 8] = [
    "duration_minutes",
    "deep_minutes",
    "light_minutes",
    "rem_minutes",
    "awake_minutes",
    "score",
    "bedtime_minutes_relative_to_wake_date",
    "wake_minutes_relative_to_wake_date",
];
const MIN_BASELINE: usize = 7;
const BASELINE_DAYS: i64 = 28;

type StoredNight = (
    Date,
    Option<Uuid>,
    String,
    Option<OffsetDateTime>,
    Option<OffsetDateTime>,
    String,
);

pub(super) async fn sleep_review_in(
    txn: &mut Transaction<'_, Postgres>,
    start: Date,
    end: Date,
    timezone: &str,
) -> Result<Value, HealthAnalysisError> {
    if HealthPeriod::new(start, end)?.days() > 31 {
        return Err(HealthAnalysisError::SleepRange);
    }
    let history_start = start
        .checked_sub(time::Duration::days(BASELINE_DAYS))
        .filter(|day| day.year() > 0)
        .ok_or(HealthAnalysisError::SleepRange)?;
    let history_end = start
        .previous_day()
        .ok_or(HealthAnalysisError::SleepRange)?;
    let stored: Vec<StoredNight> = sqlx::query_as(include_str!("sleep_review.sql"))
        .bind(history_start)
        .bind(end)
        .bind(PARSER_VERSION)
        .bind(start)
        .fetch_all(&mut **txn)
        .await?;
    let nights = stored
        .into_iter()
        .map(|row| night_document(row, timezone, start))
        .collect::<Result<Vec<_>, _>>()?;
    let (history, selected): (Vec<_>, Vec<_>) =
        nights.into_iter().partition(|(day, _)| *day < start);
    let baseline: Map<String, Value> = METRICS
        .into_iter()
        .map(|metric| {
            let values = history
                .iter()
                .map(|(_, night)| night["values"][metric].as_f64());
            (metric.to_owned(), describe_baseline(values))
        })
        .collect();
    let naps = reported_naps(txn, start, end).await?;
    let mut rows: Vec<Value> = Vec::new();
    for (day, mut night) in selected {
        if let Some(nap) = naps.get(&day) {
            night["reported_naps"] = nap.clone();
        }
        let deviations = METRICS.into_iter().filter_map(|metric| {
            let value = night["values"][metric].as_f64()?;
            let profile = &baseline[metric];
            let median = profile["median"].as_f64()?;
            let lower = profile["lower"].as_f64()?;
            let upper = profile["upper"].as_f64()?;
            let direction = if value < lower { "below" } else if value > upper { "above" } else { return None };
            Some(json!({"metric":metric,"direction":direction,"difference_from_median":value-median}))
        }).collect::<Vec<_>>();
        night["deviations"] = json!(deviations);
        if let Some(previous) = rows.last() {
            let differences: Map<String, Value> = METRICS
                .into_iter()
                .filter_map(|metric| {
                    let delta =
                        night["values"][metric].as_f64()? - previous["values"][metric].as_f64()?;
                    Some((metric.into(), json!(delta)))
                })
                .collect();
            night["change_from_previous_requested_night"] = json!(differences);
        }
        rows.push(night);
    }
    Ok(json!({
        "algorithm_version":"sleep-review-v1","timezone":timezone,
        "rows":rows,
        "baseline":{"start":history_start.to_string(),"end":history_end.to_string(),
            "metrics":baseline,"minimum_nights_for_deviation":MIN_BASELINE}
    }))
}

fn night_document(
    (day, source, values, start, end, raw_timing): StoredNight,
    timezone: &str,
    start_of_review: Date,
) -> Result<(Date, Value), HealthAnalysisError> {
    let mut values: Value =
        serde_json::from_str(&values).map_err(|_| HealthAnalysisError::Database)?;
    let [calendar, raw_start, raw_end]: [Value; 3] =
        serde_json::from_str(&raw_timing).map_err(|_| HealthAnalysisError::Database)?;
    let interval = start.zip(end).and_then(|(start, end)| {
        let (start, end) = (
            StoredInstant::try_from(start).ok()?,
            StoredInstant::try_from(end).ok()?,
        );
        if start >= end
            || (day >= start_of_review
                && (gmt_instant(&raw_start)? != start
                    || gmt_instant(&raw_end)? != end
                    || (!calendar.is_null() && calendar.as_str()? != day.to_string())))
        {
            return None;
        }
        let local_minute = |instant| {
            let local = source_offset_datetime(instant, timezone)?;
            Some(
                (local.date() - day).whole_days() * 1440
                    + i64::from(local.hour()) * 60
                    + i64::from(local.minute()),
            )
        };
        values[METRICS[6]] = json!(local_minute(start));
        values[METRICS[7]] = json!(local_minute(end));
        Some(json!({"start":start.to_string(),"end":end.to_string()}))
    });
    let available = interval.is_some()
        || values
            .as_object()
            .is_some_and(|metrics| !metrics.is_empty());
    Ok((
        day,
        json!({"day":day.to_string(),"available":available,"source_ref":source.map(|id| id.to_string()),"values":values,"main_interval":interval}),
    ))
}

fn describe_baseline(values: impl Iterator<Item = Option<f64>>) -> Value {
    let mut values: Vec<_> = values.flatten().filter(|value| value.is_finite()).collect();
    let profile = describe(values.iter().copied().map(Some));
    values.sort_unstable_by(f64::total_cmp);
    let range = (values.len() >= MIN_BASELINE).then(|| {
        let trim = values.len() / 10;
        (values[trim], values[values.len() - 1 - trim])
    });
    json!({"nights":profile.n,"median":profile.median,
        "lower":range.map(|(low, _)| low),"upper":range.map(|(_, high)| high)})
}
