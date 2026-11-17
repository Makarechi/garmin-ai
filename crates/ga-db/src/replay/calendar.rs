use std::{error::Error, fmt, sync::Arc};

use ga_archive::{Archive, ArchiveError};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    GARMIN_PARSER_VERSION, PreparedReplay, ReadReplayInput, ReplayContent, ReplayInputError,
    ReplaySkip, ReplaySource,
    input::{authorize_replay, read_input, require_parser},
};
use crate::{
    Database,
    activity_documents::{
        ApplyActivity, DocumentError, DocumentStatus, Projection, invalidate_replay_outputs,
        put_state, raw_status, read_state,
    },
    health_projection::rebuild_calendar,
    raw_capture::{GARMIN_LEGACY_SOURCE, lock_legacy_scope},
};

const MUTATION_LOCK: i64 = 72104619;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CalendarReplayOutcome {
    Skipped(ReplaySkip),
    Applied(DocumentStatus),
    Failed,
}

#[derive(Debug)]
pub enum CalendarReplayError {
    Preparation(ReplayInputError),
    Busy,
    Cancelled,
    OtherEndpoint,
    InvalidState,
    Publication(DocumentError),
    Database,
}

impl fmt::Display for CalendarReplayError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Preparation(_) => "Calendar replay input is unavailable",
            Self::Busy => "Calendar replay overlaps another storage mutation",
            Self::Cancelled => "Calendar replay was cancelled",
            Self::OtherEndpoint => "This raw source requires another replay handler",
            Self::InvalidState => "Recorded replay state is inconsistent",
            Self::Publication(_) => "Calendar projection could not be restored",
            Self::Database => "Calendar replay storage is unavailable",
        })
    }
}
impl Error for CalendarReplayError {}

impl From<sqlx::Error> for CalendarReplayError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl From<ReplayInputError> for CalendarReplayError {
    fn from(error: ReplayInputError) -> Self {
        Self::Preparation(error)
    }
}

impl From<DocumentError> for CalendarReplayError {
    fn from(error: DocumentError) -> Self {
        Self::Publication(error)
    }
}

impl Database {
    /// Rebuild one date-keyed Garmin projection from its locally verified source.
    /// The historical projection, its outcome and the analysis fence commit together.
    pub async fn replay_calendar(
        &self,
        request: ReadReplayInput<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<CalendarReplayOutcome, CalendarReplayError> {
        require_parser(request.target_parser_version)?;
        let mut txn = self.work.begin().await?;
        authorize_replay(&mut txn, request.authenticated_fingerprint).await?;
        let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !acquired {
            return Err(CalendarReplayError::Busy);
        }
        if cancelled() {
            return Err(CalendarReplayError::Cancelled);
        }
        let input = read_input(&mut txn, request, archive, cancelled.clone()).await;
        let mut source_ref = None;
        let mut error_type = None;
        let outcome = match input {
            Ok(PreparedReplay::Skip(skip)) => {
                if skip == ReplaySkip::Unchanged {
                    source_ref = Some(request.source_id);
                }
                CalendarReplayOutcome::Skipped(skip)
            }
            Ok(PreparedReplay::Ready(input)) => {
                let endpoint = input.source.endpoint.as_str();
                if !matches!(
                    endpoint,
                    "daily" | "hydration" | "sleep" | "body_battery" | "readiness" | "max_metrics"
                ) {
                    return Err(CalendarReplayError::OtherEndpoint);
                }
                let ReplayContent::Json {
                    payload,
                    timezone,
                    replacement: None,
                } = input.content
                else {
                    return Err(CalendarReplayError::OtherEndpoint);
                };
                source_ref = Some(input.source.id);
                let applied = ApplyActivity {
                    account_fingerprint: request.authenticated_fingerprint,
                    source_key: &input.source.source_key,
                    projection: Projection::Health(endpoint, None),
                    source_id: input.source.id,
                    requested_at: input.requested_at,
                    timezone: &timezone,
                };
                let (status, failure) =
                    apply_calendar(&mut txn, &input.source, &applied, &payload).await?;
                error_type = failure;
                CalendarReplayOutcome::Applied(status)
            }
            Err(error) => {
                error_type = Some(permanent_preflight_error(error)?);
                CalendarReplayOutcome::Failed
            }
        };
        if cancelled() {
            return Err(CalendarReplayError::Cancelled);
        }
        let at = database_now(&mut txn).await?;
        record_outcome(&mut txn, request, outcome, source_ref, error_type, at).await?;
        txn.commit().await?;
        Ok(outcome)
    }
}

fn permanent_preflight_error(error: ReplayInputError) -> Result<&'static str, CalendarReplayError> {
    match error {
        ReplayInputError::Archive(ArchiveError::ReadCancelled) => {
            Err(CalendarReplayError::Cancelled)
        }
        ReplayInputError::Archive(ArchiveError::Io(ref error))
            if error.kind() == std::io::ErrorKind::NotFound =>
        {
            Ok("FileNotFoundError")
        }
        ReplayInputError::Archive(
            ArchiveError::ExistingObjectDigestMismatch
            | ArchiveError::PayloadTooLarge
            | ArchiveError::InvalidObject,
        )
        | ReplayInputError::InvalidArchiveKey
        | ReplayInputError::ArchiveHashMismatch
        | ReplayInputError::HistoricalTimezone
        | ReplayInputError::HistoricalClock
        | ReplayInputError::HistoricalContract => Ok("ValueError"),
        ReplayInputError::InvalidPayload => Ok("JSONDecodeError"),
        error => Err(error.into()),
    }
}

struct Watermark {
    current: Value,
    attempt: Value,
    owns_attempt: bool,
    owns_current: bool,
    preserve_attempt: bool,
    retained: bool,
    stale: bool,
}

impl Watermark {
    fn read(
        mut current: Value,
        reference: &str,
        at: StoredInstant,
    ) -> Result<Self, CalendarReplayError> {
        let mut attempt = current["latest_attempt"].take();
        if current["status"] == "empty"
            && (attempt.is_null() || attempt.as_object().is_some_and(serde_json::Map::is_empty))
        {
            attempt = Value::Object(
                ["source_ref", "hash", "requested_at", "status"]
                    .into_iter()
                    .filter_map(|name| {
                        current
                            .get(name)
                            .map(|value| (name.to_owned(), value.clone()))
                    })
                    .collect(),
            );
        }
        let owns_attempt = attempt["source_ref"].as_str() == Some(reference);
        let owns_current = current["source_ref"].as_str() == Some(reference);
        let last_attempt = stored_clock(&attempt["requested_at"])?;
        let last_current = stored_clock(&current["requested_at"])?;
        let preserve_attempt = !owns_attempt && last_attempt.is_some_and(|last| at <= last);
        let retained =
            !owns_attempt && !owns_current && last_current.is_some_and(|last| at <= last);
        let stale = !preserve_attempt
            && !retained
            && last_attempt.or(last_current).is_some_and(|last| at < last);
        if !attempt.is_null() {
            current["latest_attempt"] = attempt.clone();
        } else if let Some(record) = current.as_object_mut() {
            record.remove("latest_attempt");
        }
        Ok(Self {
            current,
            attempt,
            owns_attempt,
            owns_current,
            preserve_attempt,
            retained,
            stale,
        })
    }
}

fn stored_clock(value: &Value) -> Result<Option<StoredInstant>, CalendarReplayError> {
    if value.is_null() || value.as_str() == Some("") {
        return Ok(None);
    }
    value
        .as_str()
        .and_then(|time| time.parse().ok())
        .map(Some)
        .ok_or(CalendarReplayError::InvalidState)
}

async fn apply_calendar(
    txn: &mut Transaction<'_, Postgres>,
    source: &ReplaySource,
    request: &ApplyActivity<'_>,
    payload: &Value,
) -> Result<(DocumentStatus, Option<&'static str>), CalendarReplayError> {
    let endpoint = source.endpoint.as_str();
    let scope = format!("{GARMIN_LEGACY_SOURCE}:{endpoint}:{}", source.source_key);
    lock_legacy_scope(txn, &scope)
        .await
        .map_err(DocumentError::Access)?;
    let key = format!("ingest:{scope}");
    let hash: String =
        sqlx::query_scalar("SELECT payload_hash FROM source_payloads WHERE id=$1 FOR UPDATE")
            .bind(source.id)
            .fetch_one(&mut **txn)
            .await?;
    let reference = source.id.to_string();
    let watermark = Watermark::read(
        Value::Object(read_state(txn, &key).await?),
        &reference,
        request.requested_at,
    )?;
    if watermark.stale {
        if source.status == "pending" {
            raw_status(txn, source.id, "stale", source.previous_parser_version).await?;
        }
        return Ok((DocumentStatus::Stale, None));
    }
    let parser_changed = source.previous_parser_version != GARMIN_PARSER_VERSION;
    let unchanged = !parser_changed
        && !matches!(source.status.as_str(), "pending" | "error")
        && watermark.current["hash"].as_str() == Some(&hash)
        && watermark.current["replacement"].is_null();
    let meta_key = format!("ingest-meta:{}", source.id);
    let mut meta = read_state(txn, &meta_key).await?;
    let requested = request.requested_at.to_string();
    let mut attempt = json!({
        "hash": hash,
        "source_ref": reference,
        "requested_at": requested,
        "replacement": null,
        "completeness": "unverified",
    });
    let mut status = source.status.clone();
    if !unchanged || matches!(endpoint, "daily" | "body_battery") {
        let mut candidate = txn.begin().await?;
        let preceding = if watermark.owns_attempt {
            watermark.current["source_ref"].as_str()
        } else if watermark.owns_current {
            meta.get("preceding_source_ref").and_then(Value::as_str)
        } else {
            None
        };
        let rebuilt = rebuild_calendar(
            &mut candidate,
            request,
            endpoint,
            payload,
            parser_changed,
            preceding,
        )
        .await;
        match rebuilt {
            Ok(new) => {
                status = new.label().to_owned();
                raw_status(&mut candidate, source.id, &status, GARMIN_PARSER_VERSION).await?;
                candidate.commit().await?;
            }
            Err(DocumentError::InvalidHealthPayload | DocumentError::InvalidRecordedState) => {
                candidate.rollback().await?;
                raw_status(txn, source.id, "error", source.previous_parser_version).await?;
                meta.insert("timezone".to_owned(), json!(request.timezone));
                meta.insert(
                    "failed_parser_version".to_owned(),
                    json!(GARMIN_PARSER_VERSION),
                );
                put_state(txn, &meta_key, &Value::Object(meta)).await?;
                if !watermark.retained {
                    attempt["parser_version"] = json!(GARMIN_PARSER_VERSION);
                    let mut state = watermark.current;
                    state["status"] = json!("error");
                    state["latest_attempt"] = if watermark.preserve_attempt {
                        watermark.attempt
                    } else {
                        attempt
                    };
                    put_state(txn, &key, &state).await?;
                }
                return Ok((DocumentStatus::Error, Some("ValueError")));
            }
            Err(error) => return Err(error.into()),
        }
    }
    if !unchanged {
        meta.insert("timezone".to_owned(), json!(request.timezone));
    }
    meta.insert("applied_at".to_owned(), json!(requested));
    meta.remove("failed_parser_version");
    if watermark.owns_attempt && !watermark.owns_current {
        meta.insert(
            "preceding_source_ref".to_owned(),
            watermark.current["source_ref"].clone(),
        );
    }
    put_state(txn, &meta_key, &Value::Object(meta)).await?;

    if !watermark.retained {
        let now = database_now(txn).await?;
        attempt["fetched_at"] = json!(now.to_string());
        attempt["status"] = json!(status);
        let state = if watermark.preserve_attempt {
            attempt["latest_attempt"] = watermark.attempt;
            attempt["status"] = watermark
                .current
                .get("status")
                .cloned()
                .unwrap_or(json!(status));
            attempt
        } else if status == "empty"
            && !watermark.owns_current
            && !watermark.current["source_ref"].is_null()
        {
            attempt["parser_version"] = json!(GARMIN_PARSER_VERSION);
            let mut state = watermark.current;
            state["status"] = json!("empty");
            state["latest_attempt"] = attempt;
            state
        } else {
            attempt
        };
        put_state(txn, &key, &state).await?;
    }

    if unchanged {
        return Ok((DocumentStatus::Unchanged, None));
    }
    invalidate_replay_outputs(txn).await?;
    let status = match status.as_str() {
        "normalized" => DocumentStatus::Normalized,
        "archived" => DocumentStatus::Archived,
        "empty" => DocumentStatus::Empty,
        _ => return Err(CalendarReplayError::InvalidState),
    };
    Ok((status, None))
}

async fn database_now(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<StoredInstant, CalendarReplayError> {
    let value: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    value
        .try_into()
        .map_err(|_| CalendarReplayError::InvalidState)
}

async fn record_outcome(
    txn: &mut Transaction<'_, Postgres>,
    request: ReadReplayInput<'_>,
    outcome: CalendarReplayOutcome,
    source_ref: Option<Uuid>,
    error_type: Option<&str>,
    at: StoredInstant,
) -> Result<(), CalendarReplayError> {
    let label = match outcome {
        CalendarReplayOutcome::Applied(status) => status.label(),
        CalendarReplayOutcome::Failed => "error",
        CalendarReplayOutcome::Skipped(skip) => match skip {
            ReplaySkip::SourceRemoved => "source_removed",
            ReplaySkip::SupersededRevision => "superseded_revision",
            ReplaySkip::UnsupportedSource => "unsupported_source",
            ReplaySkip::Unchanged => "unchanged",
        },
    };
    let mut value = json!({
        "status": label,
        "target_version": request.target_parser_version,
        "at": at.to_string(),
    });
    if let Some(reference) = source_ref {
        value["source_ref"] = json!(reference.to_string());
    }
    if let Some(error_type) = error_type {
        value["error_type"] = json!(error_type);
    }
    let key = format!(
        "replay:{}:{}",
        request.source_id, request.target_parser_version
    );
    put_state(txn, &key, &value).await?;
    Ok(())
}
