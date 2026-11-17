mod daily;
mod observations;
mod replacement;
mod samples;

use ga_domain::garmin_health::{
    HealthProjection, HealthReplacement, HealthStatus, normalize_health_payload,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};

use crate::{
    activity_documents::{ApplyActivity, DocumentError, supersede_insights},
    system_metrics::ensure_system_metrics_in,
};

pub(super) async fn publish(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    endpoint: &str,
    unchanged: bool,
    attestation: Option<&HealthReplacement>,
) -> Result<HealthStatus, DocumentError> {
    let document: String =
        sqlx::query_scalar("SELECT payload::text FROM source_payloads WHERE id = $1")
            .bind(request.source_id)
            .fetch_one(&mut **txn)
            .await?;
    let document: Value =
        serde_json::from_str(&document).map_err(|_| DocumentError::InvalidHealthPayload)?;
    let projection = normalize_health_payload(endpoint, request.source_key, &document)
        .map_err(|_| DocumentError::InvalidHealthPayload)?;
    let status = projection.status;
    let attestation = attestation.filter(|_| !unchanged);
    if let Some(attestation) = attestation {
        replacement::begin(txn, request, endpoint, attestation).await?;
    }
    let mut changed = publish_projection(
        txn,
        request,
        endpoint,
        projection,
        unchanged,
        true,
        attestation,
    )
    .await?;
    if !unchanged && (status == HealthStatus::Normalized || attestation.is_some()) {
        samples::record_application(txn, request, endpoint, &document, attestation).await?;
    }
    if let Some(attestation) = attestation {
        changed |= replacement::changed(txn, attestation).await?;
    }
    if changed {
        supersede_insights(txn).await?;
    }
    Ok(status)
}

pub(crate) async fn rebuild_calendar(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    endpoint: &str,
    document: &Value,
    parser_changed: bool,
    retained: bool,
) -> Result<HealthStatus, DocumentError> {
    let projection = normalize_health_payload(endpoint, request.source_key, document)
        .map_err(|_| DocumentError::InvalidHealthPayload)?;
    if !projection.samples.is_empty() {
        return Err(DocumentError::InvalidHealthPayload);
    }
    let mut publish_sleep = true;
    if let Some(day) = projection.day {
        if parser_changed {
            let has_samples: bool = sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM measurements WHERE source_ref=$1)",
            )
            .bind(request.source_id)
            .fetch_one(&mut **txn)
            .await?;
            if has_samples {
                return Err(DocumentError::InvalidRecordedState);
            }
            daily::clear_owned_fields(txn, day, request.source_id).await?;
            observations::preserve_for_replay(txn, request.source_id).await?;
        }
        if endpoint == "sleep" {
            publish_sleep =
                daily::prepare_sleep_replay(txn, day, request.source_id, parser_changed, retained)
                    .await?;
        }
    }
    let status = projection.status;
    if publish_projection(
        txn,
        request,
        endpoint,
        projection,
        false,
        publish_sleep,
        None,
    )
    .await?
    {
        supersede_insights(txn).await?;
    }
    Ok(status)
}

async fn publish_projection(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    endpoint: &str,
    projection: HealthProjection,
    unchanged: bool,
    publish_sleep: bool,
    attestation: Option<&HealthReplacement>,
) -> Result<bool, DocumentError> {
    if projection.status != HealthStatus::Normalized {
        return Ok(false);
    }
    let day = projection.day.ok_or(DocumentError::InvalidHealthPayload)?;
    let mut changed = daily::publish_fields(txn, request, day, endpoint, projection.fields).await?;
    if let Some((start, end)) = projection.sleep_interval.filter(|_| publish_sleep) {
        changed |= daily::publish_sleep_interval(txn, request, day, start, end).await?;
    }
    if !unchanged && (!projection.samples.is_empty() || !projection.observations.is_empty()) {
        let versions = ensure_system_metrics_in(txn)
            .await
            .map_err(|_| DocumentError::InvalidRecordedState)?;
        changed |= samples::publish_measurements(
            txn,
            request,
            &projection.samples,
            &versions,
            attestation,
        )
        .await?;
        changed |= observations::publish_observations(
            txn,
            request,
            day,
            &projection.observations,
            &versions,
        )
        .await?;
    }
    Ok(changed)
}
