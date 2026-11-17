mod journal;
mod staging;

use std::sync::Arc;

use ga_archive::Archive;
use ga_domain::garmin_health::{HealthReplacement, HealthStatus, normalize_health_payload};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};

use super::{CalendarReplayError, ReplaySource, input::legacy_application_order_unknown};
use crate::{
    activity_documents::{ApplyActivity, DocumentError},
    health_projection::{
        rebuild_intraday_view, record_sample_application, remove_attested_samples,
    },
    system_metrics::ensure_system_metrics_in,
};

pub(super) struct Rebuild<'a, C> {
    pub source: &'a ReplaySource,
    pub request: &'a ApplyActivity<'a>,
    pub payload: &'a Value,
    pub proof: Option<&'a HealthReplacement>,
    pub archive: Arc<Archive>,
    pub byte_limit: u64,
    pub cancelled: C,
    pub parser_changed: bool,
    pub retained: bool,
    pub preceding: Option<&'a str>,
}

pub(super) async fn rebuild<C>(
    txn: &mut Transaction<'_, Postgres>,
    input: Rebuild<'_, C>,
) -> Result<HealthStatus, CalendarReplayError>
where
    C: Fn() -> bool + Clone + Send + 'static,
{
    let endpoint = input.source.endpoint.as_str();
    let key = input.source.source_key.as_str();
    let mut projection = normalize_health_payload(endpoint, key, input.payload)
        .map_err(|_| DocumentError::InvalidHealthPayload)?;
    let owned = staging::snapshot(txn, input.source.id).await?;
    let history = journal::load(txn, endpoint, key).await?;
    if owned
        && history.is_none()
        && legacy_application_order_unknown(txn, input.source, input.byte_limit).await?
    {
        staging::unknown_order(txn).await?;
    }

    let mut order = Map::new();
    let mut replacements = Vec::new();
    for (position, entry) in history.unwrap_or_default().into_iter().enumerate() {
        if (input.cancelled)() {
            return Err(CalendarReplayError::Cancelled);
        }
        let journal::Application::Verified {
            raw_id,
            at,
            timezone,
            replacement,
        } = entry
        else {
            if owned {
                staging::unknown_order(txn).await?;
            }
            continue;
        };
        order.insert(raw_id.to_string(), json!(position));
        if raw_id == input.source.id {
            replacements.clear();
        } else if let Some(proof) = &replacement {
            replacements.push(json!({
                "start": proof.start().to_string(),
                "end_time": proof.end().to_string(),
                "metrics": proof.metrics(),
            }));
        }
        if !owned {
            continue;
        }
        if let Some(proof) = &replacement {
            staging::forget_covered(txn, proof).await?;
        }
        let document = journal::read_document(
            txn,
            raw_id,
            endpoint,
            key,
            Arc::clone(&input.archive),
            input.byte_limit,
            input.cancelled.clone(),
        )
        .await?;
        let previous = normalize_health_payload(endpoint, key, &document)
            .map_err(|_| DocumentError::InvalidHealthPayload)?;
        staging::record(txn, &previous.samples, at, &timezone).await?;
    }
    if owned {
        let versions = ensure_system_metrics_in(txn)
            .await
            .map_err(|_| DocumentError::InvalidRecordedState)?;
        staging::replace_owned(txn, input.source.id, input.parser_changed, &versions).await?;
    }
    if let Some(proof) = input.proof.filter(|_| !input.retained) {
        remove_attested_samples(txn, input.request, endpoint, proof).await?;
    }
    projection.samples = staging::admit(
        txn,
        projection.samples,
        input.source.id,
        &Value::Object(order),
        &Value::Array(replacements),
    )
    .await?;
    let outcome = rebuild_intraday_view(
        txn,
        input.request,
        endpoint,
        projection,
        input.parser_changed,
        input.preceding,
    )
    .await?;
    staging::retain_replay_result(txn, input.request).await?;
    record_sample_application(
        txn,
        input.request,
        endpoint,
        input.payload,
        input.proof,
        true,
    )
    .await?;
    Ok(outcome)
}
