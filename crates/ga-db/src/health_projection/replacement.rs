use ga_domain::garmin_health::HealthReplacement;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use crate::{
    activity_documents::{ApplyActivity, DocumentError},
    raw_capture::GARMIN_LEGACY_SOURCE,
};

pub(super) async fn begin(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    endpoint: &str,
    replacement: &HealthReplacement,
) -> Result<(), DocumentError> {
    // Compare the finished projection, rather than each delete and reinsert.
    // PostgreSQL can spill this bounded interval without sending old samples to the app.
    sqlx::query(
        "CREATE TEMP TABLE ga_replacement_before ON COMMIT DROP AS
         SELECT measurement.ts,measurement.metric,measurement.source,measurement.value,
                measurement.unit,measurement.local_date
         FROM measurements AS measurement
         LEFT JOIN source_payloads AS raw ON raw.id=measurement.source_ref
         WHERE (measurement.source=$1 OR raw.source=$1)
           AND measurement.metric=ANY($2::text[]) AND measurement.ts >= $3 AND measurement.ts < $4",
    )
    .bind(GARMIN_LEGACY_SOURCE)
    .bind(replacement.metrics())
    .bind(OffsetDateTime::from(replacement.start()))
    .bind(OffsetDateTime::from(replacement.end()))
    .execute(&mut **txn)
    .await?;
    sqlx::query(include_str!("replace_samples.sql"))
        .bind(GARMIN_LEGACY_SOURCE)
        .bind(endpoint)
        .bind(request.source_key)
        .bind(replacement.metrics())
        .bind(OffsetDateTime::from(replacement.start()))
        .bind(OffsetDateTime::from(replacement.end()))
        .bind(OffsetDateTime::from(request.requested_at))
        .execute(&mut **txn)
        .await?;
    Ok(())
}

pub(super) async fn changed(
    txn: &mut Transaction<'_, Postgres>,
    replacement: &HealthReplacement,
) -> Result<bool, DocumentError> {
    Ok(sqlx::query_scalar(
        "WITH current AS MATERIALIZED (
             SELECT measurement.ts,measurement.metric,measurement.source,measurement.value,
                    measurement.unit,measurement.local_date
             FROM measurements AS measurement
             LEFT JOIN source_payloads AS raw ON raw.id=measurement.source_ref
             WHERE (measurement.source=$1 OR raw.source=$1)
               AND measurement.metric=ANY($2::text[]) AND measurement.ts >= $3 AND measurement.ts < $4
         )
         SELECT EXISTS (SELECT * FROM current EXCEPT SELECT * FROM pg_temp.ga_replacement_before)
             OR EXISTS (SELECT * FROM pg_temp.ga_replacement_before EXCEPT SELECT * FROM current)",
    )
    .bind(GARMIN_LEGACY_SOURCE)
    .bind(replacement.metrics())
    .bind(OffsetDateTime::from(replacement.start()))
    .bind(OffsetDateTime::from(replacement.end()))
    .fetch_one(&mut **txn)
    .await?)
}
