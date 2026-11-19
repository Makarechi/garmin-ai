use ga_domain::{
    access::{Scope, Scopes},
    timestamp::{OffsetTimestamp, StoredInstant},
    tracker_nl::TrackerTextRequest,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};

use super::TrackerTextError;
use crate::Database;

pub(crate) enum TrackerTextOperation {
    Busy,
    Receipt(Value),
    Pending,
}

impl Database {
    pub(crate) async fn tracker_text_operation_in(
        txn: &mut Transaction<'_, Postgres>,
        request: &TrackerTextRequest,
        actor: &str,
        scopes: Scopes,
    ) -> Result<TrackerTextOperation, TrackerTextError> {
        let operation = request.operation_key(actor);
        let locked: bool =
            sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(72104623,hashtext($1))")
                .bind(&operation)
                .fetch_one(&mut **txn)
                .await?;
        if !locked {
            return Ok(TrackerTextOperation::Busy);
        }
        let receipt: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                .bind(&operation)
                .fetch_optional(&mut **txn)
                .await?;
        let Some(receipt) = receipt else {
            return Ok(TrackerTextOperation::Pending);
        };
        let receipt: Value =
            serde_json::from_str(&receipt).map_err(|_| TrackerTextError::Database)?;
        if !receipt["request_hash"].as_str().is_some_and(|hash| {
            request
                .request_hashes()
                .is_ok_and(|hashes| hashes.iter().any(|wanted| wanted == hash))
        }) {
            return Err(TrackerTextError::Invalid);
        }
        let result = receipt
            .get("result")
            .cloned()
            .ok_or(TrackerTextError::Database)?;
        if result["written"] == true && !scopes.permits(&[Scope::ReadDiary, Scope::WriteDiary]) {
            return Err(TrackerTextError::Forbidden);
        }
        Ok(TrackerTextOperation::Receipt(result))
    }

    pub(crate) async fn tracker_text_clock_in(
        txn: &mut Transaction<'_, Postgres>,
        request: &TrackerTextRequest,
        actor: &str,
        message_at: StoredInstant,
        timezone: &str,
    ) -> Result<(StoredInstant, String), TrackerTextError> {
        let key = request
            .operation_key(actor)
            .replacen("nl-operation:", "nl-context:", 1);
        let hashes = request
            .request_hashes()
            .map_err(|_| TrackerTextError::Invalid)?;
        let pinned: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                .bind(&key)
                .fetch_optional(&mut **txn)
                .await?;
        if let Some(pinned) = pinned {
            let pinned: Value =
                serde_json::from_str(&pinned).map_err(|_| TrackerTextError::Database)?;
            if !pinned["request_hash"]
                .as_str()
                .is_some_and(|hash| hashes.iter().any(|wanted| wanted == hash))
            {
                return Err(TrackerTextError::Invalid);
            }
            let message_at = StoredInstant::from_legacy_input(&pinned["message_at"])
                .ok_or(TrackerTextError::Database)?;
            let timezone = pinned["timezone"]
                .as_str()
                .ok_or(TrackerTextError::Database)?;
            return Ok((message_at, timezone.to_owned()));
        }
        let value = json!({"request_hash":hashes[0],"message_at":OffsetTimestamp(message_at.into()).to_string(),"timezone":timezone});
        sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
            .bind(key)
            .bind(value.to_string())
            .execute(&mut **txn)
            .await?;
        Ok((message_at, timezone.to_owned()))
    }
}
