use ga_domain::event_input::source_calendar_date;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{
    DiaryWriteError, relation_id,
    revisions::{snapshot_instant, snapshot_text},
    stored_instant,
};

const DURATION_FIELD: &str = "__derived_duration_minutes_v1__";

pub(super) struct ExpectedMetric {
    pub field: String,
    pub version: Uuid,
    pub key: String,
    pub number: Option<f64>,
    pub text: Option<String>,
    pub boolean: Option<bool>,
    pub unit: String,
    pub sequence: i32,
}

pub(super) struct ExpectedMetrics<'a> {
    pub values: Vec<ExpectedMetric>,
    pub start: OffsetDateTime,
    pub end: Option<OffsetDateTime>,
    pub recorded_at: OffsetDateTime,
    pub timezone: &'a str,
    pub day: Date,
}

#[expect(
    clippy::cast_precision_loss,
    reason = "Event duration is expressed in fractional minutes with microsecond input precision"
)]
pub(super) async fn expected_metrics<'a>(
    txn: &mut Transaction<'_, Postgres>,
    snapshot: &'a Value,
) -> Result<Option<ExpectedMetrics<'a>>, DiaryWriteError> {
    if snapshot["definition_version_id"].is_null() {
        return Ok(None);
    }
    let definition = relation_id(&snapshot["definition_version_id"])?;
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
        return Ok(None);
    }
    let start =
        snapshot_instant(snapshot, "start")?.ok_or(DiaryWriteError::InvalidRecordedState)?;
    let end = snapshot_instant(snapshot, "end")?;
    let recorded_at =
        snapshot_instant(snapshot, "recorded_at")?.ok_or(DiaryWriteError::InvalidRecordedState)?;
    let timezone = snapshot_text(snapshot, "timezone")?;
    let day = source_calendar_date(stored_instant(start)?, timezone)
        .ok_or(DiaryWriteError::InvalidMetricMapping)?;
    let mut values = Vec::with_capacity(mappings.len());
    for (sequence, (field, name, version, key, kind, unit, low, high)) in
        mappings.into_iter().enumerate()
    {
        let duration;
        let value = if field == DURATION_FIELD {
            duration = end
                .map(|end| ((end - start).whole_microseconds() as f64 / 1_000_000.0) / 60.0)
                .map(Value::from);
            duration.as_ref()
        } else {
            let name = name.ok_or(DiaryWriteError::InvalidMetricMapping)?;
            snapshot["payload"].get(&name)
        };
        let Some(value) = value.filter(|value| !value.is_null()) else {
            continue;
        };
        let (number, text, boolean) = metric_value(&kind, value, low, high)?;
        let sequence =
            i32::try_from(sequence).map_err(|_| DiaryWriteError::InvalidMetricMapping)?;
        values.push(ExpectedMetric {
            field,
            version,
            key,
            number,
            text: text.map(str::to_owned),
            boolean,
            unit: unit
                .filter(|unit| !unit.is_empty())
                .unwrap_or_else(|| "1".into()),
            sequence,
        });
    }
    Ok(Some(ExpectedMetrics {
        values,
        start,
        end,
        recorded_at,
        timezone,
        day,
    }))
}

pub(super) async fn project_event(
    txn: &mut Transaction<'_, Postgres>,
    source: Uuid,
    snapshot: &Value,
    transition_at: OffsetDateTime,
) -> Result<(), DiaryWriteError> {
    let Some(expected) = expected_metrics(txn, snapshot).await? else {
        return Ok(());
    };
    for metric in expected.values {
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
        .bind(metric.key)
        .bind(metric.number)
        .bind(metric.text)
        .bind(metric.boolean)
        .bind(metric.unit)
        .bind(metric.version)
        .bind(source)
        .bind(metric.field)
        .bind(expected.start)
        .bind(expected.end)
        .bind(expected.recorded_at)
        .bind(expected.day)
        .bind(transition_at)
        .bind(expected.timezone)
        .bind(metric.sequence)
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
