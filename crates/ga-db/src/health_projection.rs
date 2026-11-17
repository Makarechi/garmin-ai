mod daily;
mod observations;
mod replacement;
mod samples;

use ga_domain::garmin_health::{HealthReplacement, HealthStatus, normalize_health_payload};
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
    let attestation = attestation.filter(|_| !unchanged);
    if let Some(attestation) = attestation {
        replacement::begin(txn, request, endpoint, attestation).await?;
    }
    let mut changed = false;
    if projection.status == HealthStatus::Normalized {
        let day = projection.day.ok_or(DocumentError::InvalidHealthPayload)?;
        changed |= daily::publish_fields(txn, request, day, endpoint, projection.fields).await?;
        if let Some((start, end)) = projection.sleep_interval {
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
    }
    if !unchanged && (projection.status == HealthStatus::Normalized || attestation.is_some()) {
        samples::record_application(txn, request, endpoint, &document, attestation).await?;
    }
    if let Some(attestation) = attestation {
        changed |= replacement::changed(txn, attestation).await?;
    }
    if changed {
        supersede_insights(txn).await?;
    }
    Ok(projection.status)
}
