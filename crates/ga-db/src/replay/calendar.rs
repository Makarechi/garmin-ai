use std::{error::Error, fmt, sync::Arc};

use ga_archive::{Archive, ArchiveError};
use ga_domain::{
    garmin_health::{HealthReplacement, normalize_health_payload},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    GARMIN_PARSER_VERSION, PreparedReplay, REPLAY_MUTATION_LOCK, ReadReplayInput, ReplayContent,
    ReplayInputError, ReplaySkip, ReplaySource,
    input::{authorize_replay, read_input, require_parser},
    intraday,
};
use crate::{
    Database,
    activity_documents::{
        ActivityDocument, ActivitySummaryKind, ApplyActivity, DocumentError, DocumentStatus,
        Projection, apply_document, invalidate_replay_outputs, put_state, raw_status, read_state,
        replay_activity_summaries,
    },
    health_projection::{rebuild_calendar, rebuild_intraday_view},
    jobs::{FailureReason, JobOutcome, JobStoreError, RecordedJobStatus, ReplayJobLease},
    raw_capture::{GARMIN_LEGACY_SOURCE, lock_legacy_scope},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CalendarReplayOutcome {
    Skipped(ReplaySkip),
    Applied(DocumentStatus),
    Failed,
}

pub struct RunHealthReplay<'a> {
    pub lease: &'a ReplayJobLease,
    pub configured_timezone: &'a str,
    pub max_json_bytes: u64,
}

pub struct HealthReplayJobResult {
    pub replay: CalendarReplayOutcome,
    pub job: RecordedJobStatus,
}

#[derive(Debug)]
pub enum CalendarReplayError {
    Preparation(ReplayInputError),
    Job(JobStoreError),
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
            Self::Preparation(_) => "Garmin replay input is unavailable",
            Self::Job(_) => "Garmin replay job is unavailable",
            Self::Busy => "Garmin replay overlaps another storage mutation",
            Self::Cancelled => "Garmin replay was cancelled",
            Self::OtherEndpoint => "This raw source requires another replay handler",
            Self::InvalidState => "Recorded replay state is inconsistent",
            Self::Publication(_) => "Garmin projection could not be restored",
            Self::Database => "Garmin replay storage is unavailable",
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

impl From<JobStoreError> for CalendarReplayError {
    fn from(error: JobStoreError) -> Self {
        Self::Job(error)
    }
}

#[derive(Clone, Copy, PartialEq)]
pub(super) enum ReplayScope {
    Calendar,
    Intraday,
    ActivityDocument,
    ActivitySummary,
}

impl ReplayScope {
    pub(super) fn for_endpoint(endpoint: &str) -> Result<Self, CalendarReplayError> {
        Ok(if HEALTH_CALENDAR_ENDPOINTS.contains(&endpoint) {
            Self::Calendar
        } else if HEALTH_INTRADAY_ENDPOINTS.contains(&endpoint) {
            Self::Intraday
        } else if ActivityDocument::for_endpoint(endpoint).is_some() {
            Self::ActivityDocument
        } else if ActivitySummaryKind::for_endpoint(endpoint).is_some() {
            Self::ActivitySummary
        } else {
            return Err(CalendarReplayError::OtherEndpoint);
        })
    }

    fn require_endpoint(
        endpoint: &str,
        required: Option<Self>,
    ) -> Result<Self, CalendarReplayError> {
        let scope = Self::for_endpoint(endpoint)?;
        if required.map_or(
            matches!(scope, Self::Calendar | Self::Intraday),
            |required| required == scope,
        ) {
            Ok(scope)
        } else {
            Err(CalendarReplayError::OtherEndpoint)
        }
    }
}

pub(crate) const HEALTH_CALENDAR_ENDPOINTS: &[&str] = &[
    "daily",
    "hydration",
    "sleep",
    "body_battery",
    "readiness",
    "max_metrics",
];
pub(crate) const HEALTH_INTRADAY_ENDPOINTS: &[&str] = &[
    "heart_rate",
    "stress",
    "hrv",
    "respiration",
    "spo2",
    "steps",
];

struct ArchiveReplay<C> {
    archive: Arc<Archive>,
    byte_limit: u64,
    cancelled: C,
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
        self.replay_json(ReplayScope::Calendar, request, archive, cancelled)
            .await
    }

    /// Rebuild one intraday response and its retained overlap history.
    pub async fn replay_intraday(
        &self,
        request: ReadReplayInput<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<CalendarReplayOutcome, CalendarReplayError> {
        self.replay_json(ReplayScope::Intraday, request, archive, cancelled)
            .await
    }

    /// Restore an activity detail, lap, zone or weather document from its verified archive.
    pub async fn replay_activity_document(
        &self,
        request: ReadReplayInput<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<CalendarReplayOutcome, CalendarReplayError> {
        self.replay_json(ReplayScope::ActivityDocument, request, archive, cancelled)
            .await
    }

    /// Restore a single Garmin activity or a retained page from its verified archive.
    pub async fn replay_activity_summary(
        &self,
        request: ReadReplayInput<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<CalendarReplayOutcome, CalendarReplayError> {
        self.replay_json(ReplayScope::ActivitySummary, request, archive, cancelled)
            .await
    }

    async fn replay_json(
        &self,
        scope: ReplayScope,
        request: ReadReplayInput<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<CalendarReplayOutcome, CalendarReplayError> {
        require_parser(request.target_parser_version)?;
        let mut txn = self.work.begin().await?;
        let outcome = replay_json_in(&mut txn, Some(scope), request, archive, cancelled).await?;
        txn.commit().await?;
        Ok(outcome)
    }

    /// Rebuild a stored Garmin health response and complete its lease together.
    pub async fn run_health_replay_job(
        &self,
        options: RunHealthReplay<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<HealthReplayJobResult, CalendarReplayError> {
        self.run_json_replay_job(options, None, archive, cancelled)
            .await
    }

    pub(super) async fn run_json_replay_job(
        &self,
        options: RunHealthReplay<'_>,
        required: Option<ReplayScope>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<HealthReplayJobResult, CalendarReplayError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        options.lease.assert_live(&mut txn).await?;
        let replay = if let Some((source_id, fingerprint)) = options.lease.source() {
            replay_json_in(
                &mut txn,
                required,
                ReadReplayInput {
                    authenticated_fingerprint: &fingerprint,
                    source_id,
                    target_parser_version: GARMIN_PARSER_VERSION,
                    configured_timezone: options.configured_timezone,
                    max_json_bytes: options.max_json_bytes,
                    max_fit_bytes: 0,
                },
                archive,
                cancelled.clone(),
            )
            .await?
        } else {
            CalendarReplayOutcome::Failed
        };
        if cancelled() {
            return Err(CalendarReplayError::Cancelled);
        }
        let result = if matches!(
            replay,
            CalendarReplayOutcome::Failed | CalendarReplayOutcome::Applied(DocumentStatus::Error)
        ) {
            JobOutcome::Failed {
                reason: FailureReason::RuntimeError,
                retry_at: None,
            }
        } else {
            JobOutcome::Completed
        };
        let job = options.lease.finish_in(&mut txn, result).await?;
        txn.commit().await?;
        Ok(HealthReplayJobResult { replay, job })
    }
}

async fn replay_json_in(
    txn: &mut Transaction<'_, Postgres>,
    required: Option<ReplayScope>,
    request: ReadReplayInput<'_>,
    archive: Arc<Archive>,
    cancelled: impl Fn() -> bool + Clone + Send + 'static,
) -> Result<CalendarReplayOutcome, CalendarReplayError> {
    authorize_replay(txn, request.authenticated_fingerprint).await?;
    let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !acquired {
        return Err(CalendarReplayError::Busy);
    }
    if cancelled() {
        return Err(CalendarReplayError::Cancelled);
    }
    let registered: Option<String> =
        sqlx::query_scalar("SELECT endpoint FROM source_payloads WHERE id=$1 AND source=$2")
            .bind(request.source_id)
            .bind(GARMIN_LEGACY_SOURCE)
            .fetch_optional(&mut **txn)
            .await?;
    if let Some(endpoint) = registered {
        ReplayScope::require_endpoint(&endpoint, required)?;
    }
    let input = read_input(txn, request, Arc::clone(&archive), cancelled.clone()).await;
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
            let scope = ReplayScope::require_endpoint(endpoint, required)?;
            let ReplayContent::Json {
                payload,
                timezone,
                replacement,
            } = input.content
            else {
                return Err(CalendarReplayError::OtherEndpoint);
            };
            let proof = replacement
                .map(|proof| {
                    HealthReplacement::new(
                        endpoint,
                        proof.start,
                        proof.end,
                        proof.metrics,
                        proof.evidence,
                    )
                    .map_err(|_| CalendarReplayError::InvalidState)
                })
                .transpose()?;
            source_ref = Some(input.source.id);
            let projection = ActivityDocument::for_endpoint(endpoint)
                .map(Projection::Document)
                .or_else(|| ActivitySummaryKind::for_endpoint(endpoint).map(Projection::Summary))
                .unwrap_or(Projection::Health(endpoint, proof.as_ref()));
            let applied = ApplyActivity {
                account_fingerprint: request.authenticated_fingerprint,
                source_key: &input.source.source_key,
                projection,
                source_id: input.source.id,
                requested_at: input.requested_at,
                timezone: &timezone,
            };
            let reader = ArchiveReplay {
                archive,
                byte_limit: request.max_json_bytes,
                cancelled: cancelled.clone(),
            };
            let (status, failure) = apply_json_projection(
                txn,
                &input.source,
                &applied,
                &payload,
                proof.as_ref(),
                scope,
                reader,
            )
            .await?;
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
    let at = database_now(txn).await?;
    record_outcome(txn, request, outcome, source_ref, error_type, at).await?;
    Ok(outcome)
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

async fn apply_json_projection<C>(
    txn: &mut Transaction<'_, Postgres>,
    source: &ReplaySource,
    request: &ApplyActivity<'_>,
    payload: &Value,
    proof: Option<&HealthReplacement>,
    scope_mode: ReplayScope,
    reader: ArchiveReplay<C>,
) -> Result<(DocumentStatus, Option<&'static str>), CalendarReplayError>
where
    C: Fn() -> bool + Clone + Send + 'static,
{
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
    let replacement = proof
        .map(HealthReplacement::document)
        .unwrap_or(Value::Null);
    let unchanged = !parser_changed
        && !matches!(source.status.as_str(), "pending" | "error")
        && watermark.current["hash"].as_str() == Some(&hash)
        && watermark.current["replacement"] == replacement;
    let meta_key = format!("ingest-meta:{}", source.id);
    let mut meta = read_state(txn, &meta_key).await?;
    let requested = request.requested_at.to_string();
    let mut attempt = json!({
        "hash": hash,
        "source_ref": reference,
        "requested_at": requested,
        "replacement": replacement,
        "completeness": if proof.is_some() { "adapter_attested" } else { "unverified" },
    });
    let mut status = source.status.clone();
    if !unchanged
        || scope_mode == ReplayScope::ActivitySummary
        || matches!(endpoint, "daily" | "body_battery" | "heart_rate")
    {
        let mut candidate = txn.begin().await?;
        let preceding = if watermark.owns_attempt {
            watermark.current["source_ref"].as_str()
        } else if watermark.owns_current {
            meta.get("preceding_source_ref").and_then(Value::as_str)
        } else {
            None
        };
        let rebuilt = match scope_mode {
            ReplayScope::Calendar => rebuild_calendar(
                &mut candidate,
                request,
                endpoint,
                payload,
                parser_changed,
                watermark.retained,
                preceding,
            )
            .await
            .map(|status| status.label())
            .map_err(CalendarReplayError::Publication),
            ReplayScope::ActivityDocument | ReplayScope::ActivitySummary => {
                replay_activity_projection(&mut candidate, request, payload).await
            }
            ReplayScope::Intraday if unchanged => {
                let mut projection =
                    normalize_health_payload(endpoint, request.source_key, payload)
                        .map_err(|_| DocumentError::InvalidHealthPayload)?;
                projection.samples.clear();
                projection.observations.clear();
                rebuild_intraday_view(
                    &mut candidate,
                    request,
                    endpoint,
                    projection,
                    false,
                    preceding,
                )
                .await
                .map(|status| status.label())
                .map_err(CalendarReplayError::Publication)
            }
            ReplayScope::Intraday => intraday::rebuild(
                &mut candidate,
                intraday::Rebuild {
                    source,
                    request,
                    payload,
                    proof,
                    archive: reader.archive,
                    byte_limit: reader.byte_limit,
                    cancelled: reader.cancelled,
                    parser_changed,
                    retained: watermark.retained,
                    preceding,
                },
            )
            .await
            .map(|status| status.label()),
        };
        match rebuilt {
            Ok(new) => {
                status = new.to_owned();
                raw_status(&mut candidate, source.id, &status, GARMIN_PARSER_VERSION).await?;
                candidate.commit().await?;
            }
            Err(error) => {
                let failure = match error {
                    CalendarReplayError::Publication(
                        DocumentError::InvalidHealthPayload
                        | DocumentError::InvalidRecordedState
                        | DocumentError::InvalidSummary,
                    ) => "ValueError",
                    CalendarReplayError::Preparation(error) => permanent_preflight_error(error)?,
                    error => return Err(error),
                };
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
                return Ok((DocumentStatus::Error, Some(failure)));
            }
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
            && proof.is_none()
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

async fn replay_activity_projection(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    payload: &Value,
) -> Result<&'static str, CalendarReplayError> {
    if payload.is_null()
        || payload.as_object().is_some_and(serde_json::Map::is_empty)
        || payload.as_array().is_some_and(Vec::is_empty)
    {
        return Ok(DocumentStatus::Empty.label());
    }
    match request.projection {
        Projection::Document(kind) => {
            apply_document(txn, request, kind.endpoint(), Some(payload)).await?;
            Ok(DocumentStatus::Archived.label())
        }
        Projection::Summary(kind) => {
            replay_activity_summaries(txn, request, kind, payload).await?;
            Ok(DocumentStatus::Normalized.label())
        }
        Projection::Health(..) => Err(CalendarReplayError::OtherEndpoint),
    }
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
