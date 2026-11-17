use std::{error::Error, fmt};

use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database,
    raw_capture::{CaptureError, GARMIN_LEGACY_SOURCE},
};

const PARSER_VERSION: i32 = 9;

#[derive(Clone, Copy)]
pub enum ActivityDocument {
    Details,
    Splits,
    TypedSplits,
    Zones,
    Weather,
}

impl ActivityDocument {
    pub const fn endpoint(self) -> &'static str {
        match self {
            Self::Details => "activity_details",
            Self::Splits => "activity_splits",
            Self::TypedSplits => "activity_typed_splits",
            Self::Zones => "activity_zones",
            Self::Weather => "activity_weather",
        }
    }
}

pub struct ApplyActivityDocument<'a> {
    pub account_fingerprint: &'a str,
    pub activity_id: &'a str,
    pub document: ActivityDocument,
    pub source_id: Uuid,
    pub requested_at: StoredInstant,
    pub timezone: &'a str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DocumentStatus {
    Archived,
    Empty,
    Unchanged,
    Stale,
    Error,
}

#[derive(Debug)]
pub enum DocumentError {
    Access(CaptureError),
    SourceMissing,
    InvalidRecordedState,
    Database,
}

impl fmt::Display for DocumentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Access(_) => "Activity document publication could not be authorized",
            Self::SourceMissing => "Captured activity document was not found in its expected scope",
            Self::InvalidRecordedState => {
                "Recorded activity document state could not be interpreted"
            }
            Self::Database => "Activity document state could not be stored",
        })
    }
}

impl Error for DocumentError {}

impl From<sqlx::Error> for DocumentError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Apply a complete captured legacy document without modifying the activity summary.
    pub async fn apply_activity_document(
        &self,
        request: ApplyActivityDocument<'_>,
    ) -> Result<DocumentStatus, DocumentError> {
        let endpoint = request.document.endpoint();
        let mut txn = self
            .begin_legacy_source(request.account_fingerprint, endpoint, request.activity_id)
            .await
            .map_err(DocumentError::Access)?;
        let (hash, status, parser, empty) = sqlx::query_as::<_, (String, String, i32, bool)>(
            "SELECT payload_hash, status, parser_version,
                        payload IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb)
                 FROM source_payloads
                 WHERE id = $1 AND source = $2 AND endpoint = $3 AND source_key = $4
                   AND payload IS NOT NULL FOR UPDATE",
        )
        .bind(request.source_id)
        .bind(GARMIN_LEGACY_SOURCE)
        .bind(endpoint)
        .bind(request.activity_id)
        .fetch_optional(&mut *txn)
        .await?
        .ok_or(DocumentError::SourceMissing)?;

        let state_key = format!(
            "ingest:{GARMIN_LEGACY_SOURCE}:{endpoint}:{}",
            request.activity_id
        );
        let meta_key = format!("ingest-meta:{}", request.source_id);
        let mut state = read_state(&mut txn, &state_key).await?;
        if last_request(&state)?.is_some_and(|latest| request.requested_at < latest) {
            if status == "pending" {
                raw_status(&mut txn, request.source_id, "stale", parser).await?;
            }
            txn.commit().await?;
            return Ok(DocumentStatus::Stale);
        }

        let unchanged = state.get("hash").and_then(Value::as_str) == Some(&hash)
            && parser == PARSER_VERSION
            && !matches!(status.as_str(), "pending" | "error")
            && state.get("replacement").is_none_or(Value::is_null);
        let mut metadata = read_state(&mut txn, &meta_key).await?;
        let requested = request.requested_at.to_string();
        let attempt = json!({
            "hash": hash,
            "source_ref": request.source_id.to_string(),
            "requested_at": requested,
            "replacement": null,
            "completeness": "unverified",
        });

        if !unchanged {
            let mut effects = txn.begin().await?;
            let applied = apply_effects(&mut effects, &request, empty, &status, parser).await;
            if applied.is_err() {
                effects.rollback().await?;
                raw_status(&mut txn, request.source_id, "error", parser).await?;
                metadata.insert("timezone".to_owned(), json!(request.timezone));
                metadata.insert("failed_parser_version".to_owned(), json!(PARSER_VERSION));
                put_state(&mut txn, &meta_key, &Value::Object(metadata)).await?;
                let mut attempt = attempt;
                attempt["parser_version"] = json!(PARSER_VERSION);
                state.insert("status".to_owned(), json!("error"));
                state.insert("latest_attempt".to_owned(), attempt);
                put_state(&mut txn, &state_key, &Value::Object(state)).await?;
                txn.commit().await?;
                return Ok(DocumentStatus::Error);
            }
            effects.commit().await?;
            metadata.insert("timezone".to_owned(), json!(request.timezone));
        }

        metadata.insert("applied_at".to_owned(), json!(requested));
        metadata.remove("failed_parser_version");
        put_state(&mut txn, &meta_key, &Value::Object(metadata)).await?;
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *txn)
            .await?;
        let fetched =
            StoredInstant::try_from(now).map_err(|_| DocumentError::InvalidRecordedState)?;
        let raw_outcome = if unchanged {
            status.as_str()
        } else if empty {
            "empty"
        } else {
            "archived"
        };
        let mut value = attempt;
        value["fetched_at"] = json!(fetched.to_string());
        value["status"] = json!(raw_outcome);
        if raw_outcome == "empty"
            && let Some(reference) = state.get("source_ref").and_then(Value::as_str)
            && !reference.is_empty()
            && reference != request.source_id.to_string()
        {
            value["parser_version"] = json!(PARSER_VERSION);
            state.insert("status".to_owned(), json!("empty"));
            state.insert("latest_attempt".to_owned(), value);
            value = Value::Object(state);
        }
        put_state(&mut txn, &state_key, &value).await?;
        txn.commit().await?;
        Ok(if unchanged {
            DocumentStatus::Unchanged
        } else if empty {
            DocumentStatus::Empty
        } else {
            DocumentStatus::Archived
        })
    }
}

fn last_request(state: &Map<String, Value>) -> Result<Option<StoredInstant>, DocumentError> {
    state
        .get("latest_attempt")
        .and_then(|attempt| attempt.get("requested_at"))
        .filter(|value| !value.is_null())
        .or_else(|| state.get("requested_at").filter(|value| !value.is_null()))
        .map(|value| {
            value
                .as_str()
                .ok_or(DocumentError::InvalidRecordedState)?
                .parse()
                .map_err(|_| DocumentError::InvalidRecordedState)
        })
        .transpose()
}

async fn read_state(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
) -> Result<Map<String, Value>, DocumentError> {
    let result: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
            .bind(key)
            .fetch_optional(&mut **txn)
            .await?;
    result
        .map(|value| serde_json::from_str(&value).map_err(|_| DocumentError::InvalidRecordedState))
        .transpose()
        .map(Option::unwrap_or_default)
}

pub(crate) async fn put_state(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    value: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state (key,value) VALUES ($1,$2::jsonb)
         ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
         WHERE app_state.value IS DISTINCT FROM excluded.value",
    )
    .bind(key)
    .bind(value.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn raw_status(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    status: &str,
    parser: i32,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE source_payloads SET status = $2, parser_version = $3 WHERE id = $1")
        .bind(id)
        .bind(status)
        .bind(parser)
        .execute(&mut **txn)
        .await?;
    Ok(())
}

async fn apply_effects(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivityDocument<'_>,
    empty: bool,
    previous_status: &str,
    previous_parser: i32,
) -> Result<(), sqlx::Error> {
    let endpoint = request.document.endpoint();
    if !empty {
        let activity: Option<String> =
            sqlx::query_scalar("SELECT id FROM activities WHERE id = $1 FOR UPDATE")
                .bind(request.activity_id)
                .fetch_optional(&mut **txn)
                .await?;
        if activity.is_some() {
            let owner_key = format!("activity-parts-owner:{}:{endpoint}", request.activity_id);
            put_state(
                txn,
                &owner_key,
                &json!({"source_ref": request.source_id.to_string()}),
            )
            .await?;
            sqlx::query("DELETE FROM activity_parts WHERE activity_id = $1 AND kind = $2")
                .bind(request.activity_id)
                .bind(endpoint)
                .execute(&mut **txn)
                .await?;
            sqlx::query(
                "INSERT INTO activity_parts (activity_id, kind, sequence, payload)
                 SELECT $1, $2, (element.ordinality - 1)::int, element.value
                 FROM source_payloads AS source
                 CROSS JOIN LATERAL jsonb_array_elements(
                     CASE WHEN jsonb_typeof(source.payload) = 'array' THEN source.payload
                          ELSE jsonb_build_array(source.payload) END
                 ) WITH ORDINALITY AS element(value, ordinality)
                 WHERE source.id = $3",
            )
            .bind(request.activity_id)
            .bind(endpoint)
            .bind(request.source_id)
            .execute(&mut **txn)
            .await?;
            supersede_insights(txn).await?;
        }
    }
    raw_status(
        txn,
        request.source_id,
        if empty { "empty" } else { "archived" },
        PARSER_VERSION,
    )
    .await?;
    if previous_parser != PARSER_VERSION
        && (previous_parser > 0 || matches!(previous_status, "error" | "stale"))
    {
        invalidate_replay_outputs(txn).await?;
    }
    Ok(())
}

async fn supersede_insights(txn: &mut Transaction<'_, Postgres>) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE insights SET status = 'superseded'
         WHERE status IN ('candidate', 'accepted', 'delivered', 'uncertain')",
    )
    .execute(&mut **txn)
    .await?;
    Ok(())
}

pub(crate) async fn invalidate_replay_outputs(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "WITH chats AS (
             UPDATE conversations SET memory_epoch = gen_random_uuid(),
                 state = state - 'analysis_turns', updated_at = now()
         ), pending AS (
             DELETE FROM app_state WHERE key LIKE 'analysis:conversation:pending%'
         ), legacy AS (
             UPDATE app_state SET value = value || '{\"turns\":[]}'::jsonb,
                 updated_at = now() WHERE key = 'analysis:conversation'
         ), prompts AS (
             UPDATE pending_questions SET status = 'cancelled' WHERE kind = 'context'
                 AND status IN ('pending', 'sending', 'sent', 'uncertain')
         )
         INSERT INTO app_state (key, value) VALUES ('replay:generation', $1::jsonb)
         ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()",
    )
    .bind(json!({"id": Uuid::new_v4().to_string()}).to_string())
    .execute(&mut **txn)
    .await?;
    supersede_insights(txn).await
}
