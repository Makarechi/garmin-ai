use ga_domain::event_input::EventInput;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::DiaryWriteError;

const DURATION_FIELD: &str = "__derived_duration_minutes_v1__";

pub(super) async fn project_event(
    txn: &mut Transaction<'_, Postgres>,
    source: Uuid,
    definition: Uuid,
    report: &EventInput,
    recorded_at: OffsetDateTime,
    transition_at: OffsetDateTime,
) -> Result<(), DiaryWriteError> {
    type Mapping = (
        String,
        Option<String>,
        Uuid,
        String,
        String,
        Option<String>,
        Option<f64>,
        Option<f64>,
    );
    let mappings = sqlx::query_as::<_, Mapping>(
        "WITH latest AS (
            SELECT DISTINCT ON (field_id) field_id, metric_definition_version_id
            FROM event_metric_mappings WHERE event_definition_version_id = $1
            ORDER BY field_id, projection_version DESC
         )
         SELECT latest.field_id,
           (SELECT fields.key FROM jsonb_each(event_version.field_metadata) AS fields
                WHERE fields.value->>'id' = latest.field_id LIMIT 1),
           metric_version.id, metric.key, metric_version.value_kind,
           metric_version.unit, metric_version.minimum, metric_version.maximum
         FROM latest
         JOIN event_definition_versions AS event_version ON event_version.id = $1
         JOIN metric_definition_versions AS metric_version ON metric_version.id = latest.metric_definition_version_id
         JOIN metric_definitions AS metric ON metric.id = metric_version.definition_id
         ORDER BY latest.field_id",
    )
    .bind(definition)
    .fetch_all(&mut **txn)
    .await?;
    if mappings.is_empty() {
        return Ok(());
    }
    let day = report
        .source_calendar_date()
        .ok_or(DiaryWriteError::InvalidMetricMapping)?;
    let start = OffsetDateTime::from(report.start());
    let end = report.end().map(OffsetDateTime::from);
    for (sequence, (field, name, version, key, kind, unit, low, high)) in
        mappings.into_iter().enumerate()
    {
        let duration;
        let value = if field == DURATION_FIELD {
            duration = report
                .end()
                .map(|end| {
                    (end.unix_microseconds() - report.start().unix_microseconds()) as f64
                        / 60_000_000.0
                })
                .map(Value::from);
            duration.as_ref()
        } else {
            let name = name.ok_or(DiaryWriteError::InvalidMetricMapping)?;
            report.payload().json().get(&name)
        };
        let Some(value) = value.filter(|value| !value.is_null()) else {
            continue;
        };
        let (number, text, boolean) = metric_value(&kind, value, low, high)?;
        let sequence =
            i32::try_from(sequence).map_err(|_| DiaryWriteError::InvalidMetricMapping)?;
        sqlx::query(
            "INSERT INTO metric_observations (id, metric, value, value_text, value_boolean, unit,
                 metric_definition_version_id, source_entry_id, field_id, projection_version,
                 observed_at, effective_start, effective_end, recorded_at, source_calendar_date,
                 source_ref, fetched_at, ingested_at, timezone, quality, valid, sequence, feature_version)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                 (SELECT coalesce(max(projection_version),0) + 1 FROM metric_observations
                    WHERE source_entry_id = $8 AND field_id = $9),
                 $10, $10, $11, $12, $13, $8, $14, $14, $15, 'observed', true, $16,
                 'event-projection-v1')",
        )
        .bind(Uuid::new_v4())
        .bind(key)
        .bind(number)
        .bind(text)
        .bind(boolean)
        .bind(unit.as_deref().filter(|unit| !unit.is_empty()).unwrap_or("1"))
        .bind(version)
        .bind(source)
        .bind(field)
        .bind(start)
        .bind(end)
        .bind(recorded_at)
        .bind(day)
        .bind(transition_at)
        .bind(report.timezone())
        .bind(sequence)
        .execute(&mut **txn)
        .await?;
    }
    Ok(())
}

pub(super) async fn invalidate(
    txn: &mut Transaction<'_, Postgres>,
    event: Uuid,
    transition: OffsetDateTime,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE metric_observations SET valid = false, invalidated_at = $2
         WHERE source_entry_id = $1 AND valid",
    )
    .bind(event)
    .bind(transition)
    .execute(&mut **txn)
    .await?;
    Ok(())
}

type StoredMetricValue<'a> = (Option<f64>, Option<&'a str>, Option<bool>);

fn metric_value<'a>(
    kind: &str,
    value: &'a Value,
    low: Option<f64>,
    high: Option<f64>,
) -> Result<StoredMetricValue<'a>, DiaryWriteError> {
    match kind {
        "boolean" => value.as_bool().map(|value| (None, None, Some(value))),
        "nominal" => value
            .as_str()
            .filter(|value| !value.is_empty() && value.chars().count() <= 500)
            .map(|value| (None, Some(value), None)),
        _ => value
            .as_f64()
            .filter(|value| {
                value.is_finite()
                    && low.is_some_and(|low| *value >= low)
                    && high.is_some_and(|high| *value <= high)
            })
            .map(|value| (Some(value), None, None)),
    }
    .ok_or(DiaryWriteError::InvalidMetricMapping)
}
