mod fit_replay;

pub use fit_replay::{FitReplayError, FitReplayOutcome};

use std::{
    error::Error,
    fmt,
    io::{Read, Seek, SeekFrom},
    sync::Arc,
};

use ga_archive::{Archive, ArchiveError, ArchiveKey, ArchiveKind, PutLimits};
use ga_domain::timestamp::StoredInstant;
use ga_fit::{ExportLimits, FitError, MAX_FIT_BYTES};
use serde_json::{Map, Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database,
    activity_documents::{invalidate_replay_outputs, put_state},
    check_readiness,
    fit_staging::{FitStageError, StagedFitProjection},
    jobs::{FailureReason, GarminJobKind, GarminJobLease, JobOutcome, JobStoreError},
    raw_capture::{
        CaptureError, GARMIN_LEGACY_SOURCE, authorize_legacy_mutation, lock_legacy_scope,
    },
};

pub(crate) const ENDPOINT: &str = "activity_fit";
const PARSER_VERSION: i32 = 9;
const ZIP_END: &[u8] = b"PK\x05\x06";
const ZIP_END_BYTES: usize = 22;
const ZIP_COMMENT_BYTES: usize = 65_535;

pub struct LegacyFitCapture<'a> {
    pub activity_id: &'a str,
    pub authenticated_fingerprint: &'a str,
    pub fetched_at: StoredInstant,
}

/// Receipt for an original export recorded under the current owner and activity.
pub struct CapturedFit {
    id: Uuid,
    key: ArchiveKey,
    activity: String,
    owner: String,
    requested_at: StoredInstant,
    bytes: u64,
}

impl CapturedFit {
    pub fn source_id(&self) -> Uuid {
        self.id
    }

    pub fn key(&self) -> &ArchiveKey {
        &self.key
    }

    fn state_key(&self) -> String {
        format!("fit-version:{}", self.activity)
    }

    fn metadata_key(&self) -> String {
        format!("ingest-meta:{}", self.id)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FitIngestOutcome {
    Normalized { rows: u32 },
    Unchanged,
    Empty,
    Stale,
    Error { reason: &'static str },
}

#[derive(Debug)]
pub enum FitIngestError {
    Access(CaptureError),
    Archive(ArchiveError),
    Stage(FitStageError),
    InputTooLarge,
    ActivityMissing,
    WrongJob,
    Lease(JobStoreError),
    SourceMissing,
    InvalidRecordedState,
    ArchiveTaskFailed,
    Database,
}

impl fmt::Display for FitIngestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Access(_) => "Activity export mutation could not be authorized",
            Self::Archive(_) => "Original activity export is unavailable",
            Self::Stage(_) => "Activity projection staging failed",
            Self::InputTooLarge => "Activity export exceeds its byte limit",
            Self::WrongJob => "Activity export does not match the leased work",
            Self::Lease(_) => "Activity export lease is no longer available",
            Self::ActivityMissing => "Activity summary must be imported first",
            Self::SourceMissing => "Activity export receipt could not be verified",
            Self::InvalidRecordedState => "Recorded activity export state could not be interpreted",
            Self::ArchiveTaskFailed => "Activity export archive task ended unexpectedly",
            Self::Database => "Activity export state could not be stored",
        })
    }
}
impl Error for FitIngestError {}

impl From<sqlx::Error> for FitIngestError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl From<CaptureError> for FitIngestError {
    fn from(error: CaptureError) -> Self {
        Self::Access(error)
    }
}

impl From<ArchiveError> for FitIngestError {
    fn from(error: ArchiveError) -> Self {
        Self::Archive(error)
    }
}

impl From<JobStoreError> for FitIngestError {
    fn from(error: JobStoreError) -> Self {
        Self::Lease(error)
    }
}

impl Database {
    /// Check the job's bound account and permissions before a provider download.
    pub async fn authorize_legacy_fit_job(
        &self,
        request: &LegacyFitCapture<'_>,
        lease: &GarminJobLease,
    ) -> Result<(), FitIngestError> {
        verify_job_scope(
            lease,
            request.activity_id,
            request.authenticated_fingerprint,
        )?;
        let mut txn = self.fit_transaction().await?;
        authorize_legacy_mutation(&mut txn, request.authenticated_fingerprint, &["training"])
            .await?;
        lease.check_live(&mut txn).await?;
        txn.commit().await?;
        Ok(())
    }

    /// Archive a leased download and atomically publish its current view and queue result.
    pub async fn ingest_leased_legacy_fit(
        &self,
        request: LegacyFitCapture<'_>,
        lease: &GarminJobLease,
        source: impl Read + Seek + Send + 'static,
        archive: Arc<Archive>,
        put_limits: PutLimits,
        export_limits: ExportLimits,
    ) -> Result<FitIngestOutcome, FitIngestError> {
        verify_job_scope(
            lease,
            request.activity_id,
            request.authenticated_fingerprint,
        )?;
        let capture = self
            .capture_export(
                request,
                source,
                Arc::clone(&archive),
                put_limits,
                Some(lease),
            )
            .await?;
        self.apply_export(&capture, archive, export_limits, Some(lease))
            .await
    }

    /// Record a failed download without erasing the last success or normalized result.
    pub async fn finish_legacy_fit_fetch_error(
        &self,
        request: LegacyFitCapture<'_>,
        lease: &GarminJobLease,
        reason: FailureReason,
        retry_at: Option<StoredInstant>,
    ) -> Result<(), FitIngestError> {
        verify_job_scope(
            lease,
            request.activity_id,
            request.authenticated_fingerprint,
        )?;
        let mut txn = self.fit_transaction().await?;
        authorize_legacy_mutation(&mut txn, request.authenticated_fingerprint, &["training"])
            .await?;
        lease.check_live(&mut txn).await?;
        record_freshness(
            &mut txn,
            request.activity_id,
            request.fetched_at,
            "fetch_error",
            None,
        )
        .await?;
        lease
            .finish_in(&mut txn, JobOutcome::Failed { reason, retry_at })
            .await?;
        txn.commit().await?;
        Ok(())
    }

    /// Archive the complete original FIT or ZIP after checking owner and consent.
    pub async fn capture_legacy_activity_export(
        &self,
        request: LegacyFitCapture<'_>,
        source: impl Read + Seek + Send + 'static,
        archive: Arc<Archive>,
        limits: PutLimits,
    ) -> Result<CapturedFit, FitIngestError> {
        self.capture_export(request, source, archive, limits, None)
            .await
    }

    async fn capture_export(
        &self,
        request: LegacyFitCapture<'_>,
        mut source: impl Read + Seek + Send + 'static,
        archive: Arc<Archive>,
        mut limits: PutLimits,
        lease: Option<&GarminJobLease>,
    ) -> Result<CapturedFit, FitIngestError> {
        limits.max_bytes = limits.max_bytes.min(MAX_FIT_BYTES);
        let mut txn = self.fit_transaction().await?;
        let state = format!("fit-version:{}", request.activity_id);
        lock_fit_scope(&mut txn, request.authenticated_fingerprint, &state).await?;
        if let Some(lease) = lease {
            lease.check_live(&mut txn).await?;
        }
        let (mut txn, key, bytes) = tokio::task::spawn_blocking(move || {
            let bytes = source.seek(SeekFrom::End(0)).map_err(ArchiveError::from)?;
            if bytes > limits.max_bytes {
                return Err(FitIngestError::InputTooLarge);
            }
            let kind = legacy_archive_kind(&mut source, bytes)?;
            let key = archive.put_reader(source, kind, limits)?;
            Ok::<_, FitIngestError>((txn, key, bytes))
        })
        .await
        .map_err(|_| FitIngestError::ArchiveTaskFailed)??;
        let summary_exists: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM activities WHERE id = $1)")
                .bind(request.activity_id)
                .fetch_one(&mut *txn)
                .await?;
        if !summary_exists {
            return Err(FitIngestError::ActivityMissing);
        }
        let digest = key.digest_hex();
        let archive_key = key.to_string();
        let id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO source_payloads (id, source, endpoint, source_key, payload_hash,
                 archive_key, fetched_at, status, parser_version)
             VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', 0)
             ON CONFLICT (source, endpoint, source_key, payload_hash) DO NOTHING RETURNING id",
        )
        .bind(Uuid::new_v4())
        .bind(GARMIN_LEGACY_SOURCE)
        .bind(ENDPOINT)
        .bind(request.activity_id)
        .bind(&digest)
        .bind(&archive_key)
        .bind(OffsetDateTime::from(request.fetched_at))
        .fetch_optional(&mut *txn)
        .await?;
        let id = match id {
            Some(id) => id,
            None => {
                let existing = sqlx::query_as::<_, (Uuid, String)>(
                    "SELECT id, archive_key FROM source_payloads
                     WHERE source = $1 AND endpoint = $2 AND source_key = $3 AND payload_hash = $4",
                )
                .bind(GARMIN_LEGACY_SOURCE)
                .bind(ENDPOINT)
                .bind(request.activity_id)
                .bind(digest)
                .fetch_one(&mut *txn)
                .await?;
                if existing.1 != archive_key {
                    return Err(FitIngestError::SourceMissing);
                }
                existing.0
            }
        };
        if let Some(lease) = lease {
            lease.check_live(&mut txn).await?;
        }
        txn.commit().await?;
        Ok(CapturedFit {
            id,
            key,
            activity: request.activity_id.to_owned(),
            owner: request.authenticated_fingerprint.to_owned(),
            requested_at: request.fetched_at,
            bytes,
        })
    }

    /// Apply a captured export into the legacy current-view rows. The previous
    /// verified result remains visible until the full replacement commits.
    pub async fn apply_legacy_activity_export(
        &self,
        capture: &CapturedFit,
        archive: Arc<Archive>,
        limits: ExportLimits,
    ) -> Result<FitIngestOutcome, FitIngestError> {
        self.apply_export(capture, archive, limits, None).await
    }

    async fn apply_export(
        &self,
        capture: &CapturedFit,
        archive: Arc<Archive>,
        limits: ExportLimits,
        lease: Option<&GarminJobLease>,
    ) -> Result<FitIngestOutcome, FitIngestError> {
        let mut txn = self.fit_transaction().await?;
        let current = LockedFit::open(&mut txn, capture, lease).await?;
        if let Some(outcome) = current.short_circuit(&mut txn, capture).await? {
            return commit_outcome(txn, capture, lease, outcome).await;
        }
        txn.commit().await?;

        let key = capture.key.clone();
        let maximum = limits.max_bytes.min(MAX_FIT_BYTES);
        let source = tokio::task::spawn_blocking(move || {
            archive.open_verified_reader(&key, maximum, || false)
        })
        .await
        .map_err(|_| FitIngestError::ArchiveTaskFailed)??;
        match self.stage_fit_projection(source, limits).await {
            Ok(staged) => publish_staged(staged, capture, lease).await,
            Err(FitStageError::Decode(error)) if permanent_decode_error(error) => {
                let mut txn = self.fit_transaction().await?;
                let current = LockedFit::open(&mut txn, capture, lease).await?;
                let early = current.short_circuit(&mut txn, capture).await?;
                let outcome = match early {
                    Some(outcome) => outcome,
                    None => {
                        current
                            .record_error(&mut txn, capture, legacy_decode_label(error))
                            .await?
                    }
                };
                commit_outcome(txn, capture, lease, outcome).await
            }
            Err(error) => Err(FitIngestError::Stage(error)),
        }
    }

    async fn fit_transaction(&self) -> Result<Transaction<'static, Postgres>, FitIngestError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(CaptureError::Storage)?;
        Ok(txn)
    }
}

fn verify_job_scope(
    lease: &GarminJobLease,
    activity: &str,
    fingerprint: &str,
) -> Result<(), FitIngestError> {
    let payload: Value =
        serde_json::from_str(lease.payload_json()).map_err(|_| FitIngestError::WrongJob)?;
    let matches_account = match payload.get("account") {
        None | Some(Value::Null) => true,
        Some(Value::String(account)) => account.is_empty() || account == fingerprint,
        _ => false,
    };
    if lease.kind() != GarminJobKind::Fit
        || payload["activity_id"].as_str() != Some(activity)
        || !matches_account
    {
        return Err(FitIngestError::WrongJob);
    }
    Ok(())
}

async fn commit_outcome(
    mut txn: Transaction<'_, Postgres>,
    capture: &CapturedFit,
    lease: Option<&GarminJobLease>,
    outcome: FitIngestOutcome,
) -> Result<FitIngestOutcome, FitIngestError> {
    if let Some(lease) = lease {
        let status = match outcome {
            FitIngestOutcome::Normalized { .. } => "normalized",
            FitIngestOutcome::Unchanged => "unchanged",
            FitIngestOutcome::Empty => "empty",
            FitIngestOutcome::Stale => "stale",
            FitIngestOutcome::Error { .. } => "error",
        };
        record_freshness(
            &mut txn,
            &capture.activity,
            capture.requested_at,
            status,
            Some(capture.id),
        )
        .await?;
        let job = if matches!(outcome, FitIngestOutcome::Error { .. }) {
            JobOutcome::Failed {
                reason: FailureReason::InvalidGarminWork,
                retry_at: None,
            }
        } else {
            JobOutcome::Completed
        };
        lease.finish_in(&mut txn, job).await?;
    }
    txn.commit().await?;
    Ok(outcome)
}

async fn record_freshness(
    txn: &mut Transaction<'_, Postgres>,
    activity: &str,
    requested_at: StoredInstant,
    status: &str,
    source: Option<Uuid>,
) -> Result<(), FitIngestError> {
    let key = format!("freshness:{ENDPOINT}:{activity}");
    let old: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
            .bind(&key)
            .fetch_optional(&mut **txn)
            .await?;
    let previous: Value = old
        .map(|value| serde_json::from_str(&value))
        .transpose()
        .map_err(|_| FitIngestError::InvalidRecordedState)?
        .unwrap_or(Value::Null);
    let last = previous["fetched_at"]
        .as_str()
        .or_else(|| previous["success_at"].as_str());
    if let Some(last) = last {
        let last = last
            .parse::<StoredInstant>()
            .map_err(|_| FitIngestError::InvalidRecordedState)?;
        if last > requested_at {
            return Ok(());
        }
    }
    let fetched = json!(requested_at.to_string());
    let success = if status == "fetch_error" {
        &previous["success_at"]
    } else {
        &fetched
    };
    let normalized = if matches!(status, "error" | "stale" | "fetch_error") {
        &previous["normalized_at"]
    } else {
        &fetched
    };
    put_state(
        txn,
        &key,
        &json!({
            "fetched_at": fetched, "success_at": success, "normalized_at": normalized,
            "status": status, "source_key": activity, "source_ref": source.map(|id| id.to_string()),
        }),
    )
    .await?;
    Ok(())
}

fn legacy_archive_kind(
    source: &mut (impl Read + Seek),
    size: u64,
) -> Result<ArchiveKind, ArchiveError> {
    let length = size.min((ZIP_END_BYTES + ZIP_COMMENT_BYTES) as u64);
    source.seek(SeekFrom::Start(size - length))?;
    let mut tail = vec![0; length as usize];
    source.read_exact(&mut tail)?;
    source.seek(SeekFrom::Start(0))?;
    let zip = tail
        .windows(ZIP_END.len())
        .rposition(|marker| marker == ZIP_END)
        .is_some_and(|start| tail.len() - start >= ZIP_END_BYTES);
    Ok(if zip {
        ArchiveKind::Zip
    } else {
        ArchiveKind::Fit
    })
}

async fn lock_fit_scope(
    txn: &mut Transaction<'_, Postgres>,
    owner: &str,
    scope: &str,
) -> Result<(), FitIngestError> {
    authorize_legacy_mutation(txn, owner, &["training"]).await?;
    lock_legacy_scope(txn, scope).await?;
    Ok(())
}

struct LockedFit {
    state: Map<String, Value>,
    source_status: String,
    source_parser: i32,
    activity_is_current: bool,
    activity_parsed_archive: bool,
}

impl LockedFit {
    async fn open(
        txn: &mut Transaction<'_, Postgres>,
        capture: &CapturedFit,
        lease: Option<&GarminJobLease>,
    ) -> Result<Self, FitIngestError> {
        let scope = capture.state_key();
        lock_fit_scope(txn, &capture.owner, &scope).await?;
        if let Some(lease) = lease {
            lease.check_live(txn).await?;
        }
        Self::load(txn, capture).await
    }

    async fn load(
        txn: &mut Transaction<'_, Postgres>,
        capture: &CapturedFit,
    ) -> Result<Self, FitIngestError> {
        let scope = capture.state_key();
        let archive_key = capture.key.to_string();
        let (activity_fit_key, activity_parsed_archive) = sqlx::query_as::<_, (bool, bool)>(
            "SELECT fit_key IS NOT DISTINCT FROM $2, details->>'parsed_fit_key' IS NOT DISTINCT FROM $2
             FROM activities WHERE id = $1 FOR UPDATE",
        )
        .bind(&capture.activity)
        .bind(&archive_key)
        .fetch_optional(&mut **txn)
        .await?
        .ok_or(FitIngestError::ActivityMissing)?;
        let (source_status, source_parser) = sqlx::query_as::<_, (String, i32)>(
            "SELECT status, parser_version FROM source_payloads
             WHERE id = $1 AND source = $2 AND endpoint = $3 AND source_key = $4 AND archive_key = $5
             FOR UPDATE",
        )
        .bind(capture.id)
        .bind(GARMIN_LEGACY_SOURCE)
        .bind(ENDPOINT)
        .bind(&capture.activity)
        .bind(archive_key)
        .fetch_optional(&mut **txn)
        .await?
        .ok_or(FitIngestError::SourceMissing)?;
        let state: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
                .bind(scope)
                .fetch_optional(&mut **txn)
                .await?;
        let state = state
            .map(|value| {
                serde_json::from_str(&value).map_err(|_| FitIngestError::InvalidRecordedState)
            })
            .transpose()?
            .unwrap_or_default();
        Ok(Self {
            state,
            source_status,
            source_parser,
            activity_is_current: activity_fit_key && activity_parsed_archive,
            activity_parsed_archive,
        })
    }

    async fn short_circuit(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        capture: &CapturedFit,
    ) -> Result<Option<FitIngestOutcome>, FitIngestError> {
        self.short_circuit_with(txn, capture, ReplayWatermark::default())
            .await
    }

    fn latest_requested(&self) -> Result<Option<StoredInstant>, FitIngestError> {
        self.state
            .get("latest_attempt")
            .and_then(|attempt| attempt.get("requested_at"))
            .filter(|value| !value.is_null())
            .or_else(|| {
                self.state
                    .get("requested_at")
                    .filter(|value| !value.is_null())
            })
            .map(|value| {
                value
                    .as_str()
                    .ok_or(FitIngestError::InvalidRecordedState)?
                    .parse::<StoredInstant>()
                    .map_err(|_| FitIngestError::InvalidRecordedState)
            })
            .transpose()
    }

    async fn short_circuit_with(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        capture: &CapturedFit,
        mode: ReplayWatermark,
    ) -> Result<Option<FitIngestOutcome>, FitIngestError> {
        if self
            .latest_requested()?
            .is_some_and(|latest| capture.requested_at < latest)
            && !mode.preserve_attempt
            && !mode.retained
        {
            if self.source_status == "pending" {
                update_raw(txn, capture.id, "stale", self.source_parser).await?;
            }
            return Ok(Some(FitIngestOutcome::Stale));
        }
        if self.activity_is_current
            && self.source_parser == PARSER_VERSION
            && self.source_status == "normalized"
        {
            self.record_success_with(txn, capture, mode).await?;
            return Ok(Some(FitIngestOutcome::Unchanged));
        }
        if capture.bytes == 0 {
            update_raw(txn, capture.id, "empty", PARSER_VERSION).await?;
            self.record_success_with(txn, capture, mode).await?;
            return Ok(Some(FitIngestOutcome::Empty));
        }
        Ok(None)
    }

    async fn record_success(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        capture: &CapturedFit,
    ) -> Result<(), FitIngestError> {
        self.record_success_with(txn, capture, ReplayWatermark::default())
            .await
    }

    async fn record_success_with(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        capture: &CapturedFit,
        mode: ReplayWatermark,
    ) -> Result<(), FitIngestError> {
        if !mode.replay
            && self.source_parser != PARSER_VERSION
            && (self.source_parser > 0 || matches!(self.source_status.as_str(), "error" | "stale"))
        {
            invalidate_replay_outputs(txn).await?;
        }
        if !mode.preserve_attempt && !mode.retained {
            put_state(
                txn,
                &capture.state_key(),
                &json!({
                    "requested_at": capture.requested_at.to_string(),
                    "source_ref": capture.id.to_string(),
                }),
            )
            .await?;
        }
        put_state(txn, &capture.metadata_key(), &json!({})).await?;
        Ok(())
    }

    async fn record_error(
        self,
        txn: &mut Transaction<'_, Postgres>,
        capture: &CapturedFit,
        reason: &'static str,
    ) -> Result<FitIngestOutcome, FitIngestError> {
        self.record_error_with(txn, capture, reason, ReplayWatermark::default())
            .await
    }

    async fn record_error_with(
        mut self,
        txn: &mut Transaction<'_, Postgres>,
        capture: &CapturedFit,
        reason: &'static str,
        mode: ReplayWatermark,
    ) -> Result<FitIngestOutcome, FitIngestError> {
        update_raw(txn, capture.id, "error", self.source_parser).await?;
        put_state(
            txn,
            &capture.metadata_key(),
            &json!({"failed_parser_version": PARSER_VERSION}),
        )
        .await?;
        if !mode.preserve_attempt && !mode.retained {
            self.state.insert(
                "latest_attempt".to_owned(),
                json!({
                    "source_ref": capture.id.to_string(),
                    "requested_at": capture.requested_at.to_string(),
                    "parser_version": PARSER_VERSION,
                }),
            );
        }
        put_state(txn, &capture.state_key(), &Value::Object(self.state)).await?;
        sqlx::query(
            "UPDATE activities SET details = details || jsonb_build_object(
                'fit_status', 'error', 'fit_error_type', $2::text, 'fit_attempt_source_ref', $3::text),
                updated_at = now()
             WHERE id = $1",
        )
        .bind(&capture.activity)
        .bind(reason)
        .bind(capture.id.to_string())
        .execute(&mut **txn)
        .await?;
        Ok(FitIngestOutcome::Error { reason })
    }
}

#[derive(Clone, Copy, Default)]
struct ReplayWatermark {
    replay: bool,
    preserve_attempt: bool,
    retained: bool,
}

async fn update_raw(
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

async fn publish_staged(
    staged: StagedFitProjection,
    capture: &CapturedFit,
    lease: Option<&GarminJobLease>,
) -> Result<FitIngestOutcome, FitIngestError> {
    let inventory = staged.inventory();
    let mut txn = staged.transaction;
    let current = LockedFit::open(&mut txn, capture, lease).await?;
    if let Some(outcome) = current.short_circuit(&mut txn, capture).await? {
        return commit_outcome(txn, capture, lease, outcome).await;
    }

    let mut replacement = txn.begin().await?;
    if replace_fit_rows(&mut replacement, capture, inventory.messages)
        .await
        .is_err()
    {
        replacement.rollback().await?;
        let outcome = current
            .record_error(&mut txn, capture, "DatabaseError")
            .await?;
        return commit_outcome(txn, capture, lease, outcome).await;
    }
    replacement.commit().await?;
    current.record_success(&mut txn, capture).await?;
    commit_outcome(
        txn,
        capture,
        lease,
        FitIngestOutcome::Normalized {
            rows: inventory.messages,
        },
    )
    .await
}

async fn replace_fit_rows(
    txn: &mut Transaction<'_, Postgres>,
    capture: &CapturedFit,
    expected: u32,
) -> Result<(), FitIngestError> {
    sqlx::query("DELETE FROM activity_parts WHERE activity_id = $1 AND left(kind, 4) = 'fit_'")
        .bind(&capture.activity)
        .execute(&mut **txn)
        .await?;
    let inserted = sqlx::query(
        "INSERT INTO activity_parts (activity_id, kind, sequence, payload)
         SELECT $1, kind, sequence, payload FROM pg_temp.fit_candidate_rows",
    )
    .bind(&capture.activity)
    .execute(&mut **txn)
    .await?;
    if inserted.rows_affected() != u64::from(expected) {
        return Err(FitIngestError::Database);
    }
    let key = capture.key.to_string();
    sqlx::query(
        "UPDATE activities SET fit_key = $2,
             details = (details - 'fit_error_type') || jsonb_build_object(
                 'fit_status', 'normalized', 'parsed_fit_key', $2::text, 'fit_attempt_source_ref', $3::text),
             updated_at = now()
         WHERE id = $1",
    )
    .bind(&capture.activity)
    .bind(key)
    .bind(capture.id.to_string())
    .execute(&mut **txn)
    .await?;
    update_raw(txn, capture.id, "normalized", PARSER_VERSION).await?;
    Ok(())
}

fn permanent_decode_error(error: FitError) -> bool {
    !matches!(
        error,
        FitError::Cancelled
            | FitError::DeadlineExceeded
            | FitError::ReadFailed
            | FitError::InvalidProfile
    )
}

fn legacy_decode_label(error: FitError) -> &'static str {
    match error {
        FitError::HeaderChecksum | FitError::FileChecksum => "FitCRCError",
        FitError::Truncated => "FitEOFError",
        FitError::InvalidHeartRateComponent => "TypeError",
        FitError::MissingHeartRateStart => "AssertionError",
        FitError::InvalidZip | FitError::UnsafeZipMember => "BadZipFile",
        FitError::UnsupportedZipCompression => "NotImplementedError",
        FitError::NoFitMembers | FitError::LimitExceeded | FitError::TooManyZipMembers => {
            "ValueError"
        }
        _ => "FitParseError",
    }
}
