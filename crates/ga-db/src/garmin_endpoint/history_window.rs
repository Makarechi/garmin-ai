use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::Endpoint;
use crate::activity_documents::DocumentStatus;

pub(super) async fn superseded(
    txn: &mut Transaction<'_, Postgres>,
    endpoint: &Endpoint,
    lease_id: Uuid,
) -> Result<bool, sqlx::Error> {
    let Some(key) = &endpoint.window else {
        return Ok(false);
    };
    sqlx::query_scalar(
        "SELECT EXISTS (
            SELECT 1 FROM app_state
            WHERE key=$1 AND NULLIF(value->>'job_id','') <> $2
        )",
    )
    .bind(key)
    .bind(lease_id.to_string())
    .fetch_one(&mut **txn)
    .await
}

pub(super) async fn disable(
    txn: &mut Transaction<'_, Postgres>,
    endpoint: &Endpoint,
    at: StoredInstant,
) -> Result<(), sqlx::Error> {
    if let Some(key) = &endpoint.window {
        sqlx::query(
            "UPDATE app_state
             SET value=value || jsonb_build_object('status','disabled','disabled_at',$2::text),
                 updated_at=now()
             WHERE key=$1 AND value->>'status'='pending'",
        )
        .bind(key)
        .bind(at.to_string())
        .execute(&mut **txn)
        .await?;
    }
    Ok(())
}

pub(super) async fn complete(
    txn: &mut Transaction<'_, Postgres>,
    endpoint: &Endpoint,
    status: DocumentStatus,
    source: Uuid,
    at: StoredInstant,
) -> Result<(), sqlx::Error> {
    let Some(key) = &endpoint.window else {
        return Ok(());
    };
    let (window_status, source_status, source_ref) =
        if matches!(status, DocumentStatus::Stale | DocumentStatus::Unchanged) {
            let state: Option<(Option<String>, Option<String>)> = sqlx::query_as(
                "SELECT value->>'status', value->>'source_ref' FROM app_state WHERE key=$1",
            )
            .bind(format!("ingest:{}", endpoint.scope()))
            .fetch_optional(&mut **txn)
            .await?;
            let (source_status, source_ref) =
                state.unwrap_or_else(|| (Some("unknown".to_owned()), None));
            let label = if status == DocumentStatus::Stale {
                "superseded"
            } else if source_status.as_deref() == Some("empty") {
                "empty"
            } else {
                "unchanged"
            };
            (label, source_status, source_ref)
        } else {
            let label = status.label();
            (label, Some(label.to_owned()), Some(source.to_string()))
        };
    let update = json!({
        "status": window_status, "source_ref": source_ref,
        "source_status": source_status, "completed_at": at.to_string(),
    });
    sqlx::query("UPDATE app_state SET value=value || $2::jsonb, updated_at=now() WHERE key=$1")
        .bind(key)
        .bind(update.to_string())
        .execute(&mut **txn)
        .await?;
    Ok(())
}
