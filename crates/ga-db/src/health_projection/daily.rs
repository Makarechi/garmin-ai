use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value, json};
use sqlx::{Postgres, QueryBuilder, Transaction};
use time::{Date, OffsetDateTime};

use crate::{
    activity_documents::{ApplyActivity, DocumentError},
    raw_capture::{GARMIN_LEGACY_SOURCE, lock_legacy_scope},
};

const DAILY_FIELDS: &[&str] = &[
    "active_calories",
    "awake_seconds",
    "body_battery_charged",
    "body_battery_drained",
    "body_battery_high",
    "body_battery_low",
    "deep_seconds",
    "hrv_baseline_high",
    "hrv_baseline_low",
    "hrv_nightly_avg",
    "hrv_status",
    "hrv_weekly_avg",
    "hydration_ml",
    "intensity_minutes",
    "light_seconds",
    "recovery_time_minutes",
    "rem_seconds",
    "resting_hr",
    "sleep_score",
    "sleep_seconds",
    "steps",
    "stress_avg",
    "stress_max",
    "training_readiness_score",
    "vo2max",
];

pub(super) async fn publish_fields(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    day: Date,
    endpoint: &str,
    mut fields: Map<String, Value>,
) -> Result<bool, DocumentError> {
    if fields.is_empty() {
        return Ok(false);
    }
    lock_legacy_scope(txn, &format!("health-day:{day}"))
        .await
        .map_err(DocumentError::Access)?;
    let prior: Option<String> = sqlx::query_scalar(
        "SELECT to_jsonb(health_days)::text FROM health_days WHERE day = $1 FOR UPDATE",
    )
    .bind(day)
    .fetch_optional(&mut **txn)
    .await?;
    let previous: Value = prior
        .map(|prior| serde_json::from_str(&prior))
        .transpose()
        .map_err(|_| DocumentError::InvalidRecordedState)?
        .unwrap_or(Value::Null);
    let old_sources = &previous["sources"];
    let mut sources = Map::new();
    let source = request.source_id.to_string();
    let applied = request.requested_at.to_string();
    let mut changed = false;
    let mut columns = Vec::new();
    for column in DAILY_FIELDS {
        let Some(value) = fields.get_mut(*column) else {
            continue;
        };
        let time_key = format!("time:{column}");
        let recorded = old_sources[&time_key]
            .as_str()
            .map(str::parse::<StoredInstant>)
            .transpose()
            .map_err(|_| DocumentError::InvalidRecordedState)?;
        if recorded.is_some_and(|recorded| request.requested_at < recorded) {
            continue;
        }
        if *column == "steps" {
            let rounded = value
                .as_f64()
                .ok_or(DocumentError::InvalidHealthPayload)?
                .round_ties_even();
            if rounded > f64::from(i32::MAX) || rounded < 0.0 {
                return Err(DocumentError::InvalidHealthPayload);
            }
            *value = json!(rounded as i32);
        }
        changed |= !equal_field(&previous[*column], value);
        sources.insert(format!("field:{column}"), json!(source));
        sources.insert(time_key, json!(applied));
        columns.push(*column);
    }
    if columns.is_empty() {
        return Ok(false);
    }
    sources.insert(format!("payload:{source}"), json!(endpoint));
    // Column names come only from the installed projection contract above.
    let updates = columns
        .iter()
        .map(|field| format!("{field} = excluded.{field}"))
        .collect::<Vec<_>>()
        .join(", ");
    let columns = columns.join(", ");
    let mut statement = QueryBuilder::<Postgres>::new("INSERT INTO health_days (day, ");
    statement
        .push(&columns)
        .push(", sources) SELECT ")
        .push_bind(day)
        .push(", ")
        .push(&columns)
        .push(", ")
        .push_bind(Value::Object(sources).to_string())
        .push("::jsonb")
        .push(" FROM jsonb_populate_record(NULL::health_days, ")
        .push_bind(Value::Object(fields).to_string())
        .push("::jsonb) ON CONFLICT (day) DO UPDATE SET ")
        .push(updates)
        .push(", sources = health_days.sources || excluded.sources, updated_at = now()");
    statement.build().execute(&mut **txn).await?;
    Ok(changed)
}

fn equal_field(previous: &Value, new_value: &Value) -> bool {
    match (previous.as_f64(), new_value.as_f64()) {
        (Some(previous), Some(new_value)) => previous == new_value,
        _ => previous == new_value,
    }
}

pub(super) async fn publish_sleep_interval(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    day: Date,
    start: StoredInstant,
    end: StoredInstant,
) -> Result<bool, DocumentError> {
    let id = format!("sleep:{day}");
    let (start, end) = (OffsetDateTime::from(start), OffsetDateTime::from(end));
    let before: Option<Value> = sqlx::query_scalar::<_, String>(
        "SELECT to_jsonb(timeline_intervals)::text FROM timeline_intervals WHERE id = $1",
    )
    .bind(&id)
    .fetch_optional(&mut **txn)
    .await?
    .map(|value| serde_json::from_str(&value))
    .transpose()
    .map_err(|_| DocumentError::InvalidRecordedState)?;
    sqlx::query("DELETE FROM app_state WHERE key = $1")
        .bind(format!("interval-owner:{id}"))
        .execute(&mut **txn)
        .await?;
    let updated = sqlx::query(
        r#"INSERT INTO timeline_intervals (id, start, "end", label, source, confidence, confirmed, evidence)
         VALUES ($1, $2, $3, 'sleep', $4, 1, true, $5::jsonb)
         ON CONFLICT(id) DO UPDATE SET start = excluded.start, "end" = excluded."end",
             label = excluded.label, source = excluded.source, confidence = 1, confirmed = true,
             evidence = excluded.evidence
         WHERE (timeline_intervals.start, timeline_intervals."end", timeline_intervals.label,
                timeline_intervals.source, timeline_intervals.confidence, timeline_intervals.confirmed,
                timeline_intervals.evidence)
            IS DISTINCT FROM
               (excluded.start, excluded."end", excluded.label, excluded.source,
                excluded.confidence, excluded.confirmed, excluded.evidence)"#,
    )
    .bind(id).bind(start).bind(end).bind(GARMIN_LEGACY_SOURCE)
    .bind(json!({"source_ref": request.source_id.to_string()}).to_string())
    .execute(&mut **txn).await?;
    Ok(updated.rows_affected() > 0
        && before.is_none_or(|before| {
            let time = |column: &str| {
                before[column]
                    .as_str()
                    .and_then(|value| value.parse::<StoredInstant>().ok())
                    .map(OffsetDateTime::from)
            };
            let extra_evidence = before["evidence"]
                .as_object()
                .is_none_or(|evidence| evidence.keys().any(|name| name != "source_ref"));
            time("start") != Some(start)
                || time("end") != Some(end)
                || before["label"] != "sleep"
                || before["source"] != GARMIN_LEGACY_SOURCE
                || before["confidence"].as_f64() != Some(1.0)
                || before["confirmed"] != true
                || extra_evidence
        }))
}
