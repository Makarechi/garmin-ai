use std::{error::Error, fmt, sync::Arc};

use ga_archive::{Archive, ArchiveError};
use ga_domain::timestamp::StoredInstant;
use ga_fit::{ExportLimits, FitError, MAX_FIT_BYTES};
use serde_json::{Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    CapturedFit, DocumentError, ENDPOINT, FitIngestError, FitIngestOutcome, FitStageError,
    LockedFit, PARSER_VERSION, ReplayWatermark, invalidate_replay_outputs, legacy_decode_label,
    permanent_decode_error, put_state, replace_fit_rows,
};
use crate::{
    Database,
    jobs::{FailureReason, JobOutcome, JobStoreError, RecordedJobStatus, ReplayJobLease},
    raw_capture::CaptureError,
    raw_capture::{GARMIN_LEGACY_SOURCE, lock_legacy_scope},
    replay::{
        PreparedReplay, REPLAY_MUTATION_LOCK, ReadReplayInput, ReplayContent, ReplayInputError,
        ReplaySkip, authorize_replay, fit_clock, read_input, require_parser, select_sources,
    },
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FitReplayOutcome {
    Skipped(ReplaySkip),
    Applied(FitIngestOutcome),
    RejectedArchive(&'static str),
}

#[derive(Debug)]
pub enum FitReplayError {
    Input(ReplayInputError),
    Publication(FitIngestError),
    Lease(JobStoreError),
    Busy,
    Cancelled,
    OtherEndpoint,
    SourceChanged,
    Database,
}

impl fmt::Display for FitReplayError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Input(_) => "FIT replay input is unavailable",
            Self::Publication(_) => "FIT replay projection could not be restored",
            Self::Lease(_) => "FIT replay job lease is unavailable",
            Self::Busy => "FIT replay overlaps another storage mutation",
            Self::Cancelled => "FIT replay was cancelled",
            Self::OtherEndpoint => "This raw source requires another replay handler",
            Self::SourceChanged => "The FIT replay source changed during verification",
            Self::Database => "FIT replay state could not be stored",
        })
    }
}
impl Error for FitReplayError {}

impl From<ReplayInputError> for FitReplayError {
    fn from(value: ReplayInputError) -> Self {
        Self::Input(value)
    }
}
impl From<FitIngestError> for FitReplayError {
    fn from(value: FitIngestError) -> Self {
        Self::Publication(value)
    }
}
impl From<JobStoreError> for FitReplayError {
    fn from(value: JobStoreError) -> Self {
        Self::Lease(value)
    }
}
impl From<sqlx::Error> for FitReplayError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

enum Candidate {
    Skip,
    Rejected {
        key: String,
        hash: String,
        reason: &'static str,
    },
    Verified {
        key: String,
        bytes: u64,
        projection: Projection,
    },
}

enum Projection {
    Current,
    Empty,
    Staged(u32),
    DecodeError(&'static str),
}

impl Database {
    /// Rebuild FIT parts from verified archive bytes without contacting Garmin.
    pub async fn replay_fit(
        &self,
        request: ReadReplayInput<'_>,
        archive: Arc<Archive>,
        limits: ExportLimits,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
    ) -> Result<FitReplayOutcome, FitReplayError> {
        self.replay_fit_in_job(request, archive, limits, cancelled, None)
            .await
            .map(|(outcome, _)| outcome)
    }

    pub(crate) async fn replay_fit_in_job(
        &self,
        mut request: ReadReplayInput<'_>,
        archive: Arc<Archive>,
        limits: ExportLimits,
        cancelled: impl Fn() -> bool + Clone + Send + 'static,
        lease: Option<&ReplayJobLease>,
    ) -> Result<(FitReplayOutcome, Option<RecordedJobStatus>), FitReplayError> {
        require_parser(request.target_parser_version)?;
        request.max_fit_bytes = request
            .max_fit_bytes
            .min(limits.max_bytes)
            .min(MAX_FIT_BYTES);
        if cancelled() {
            return Err(FitReplayError::Cancelled);
        }
        let mut inspect = self
            .work
            .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await?;
        authorize_replay(&mut inspect, request.authenticated_fingerprint).await?;
        if let Some(lease) = lease {
            lease.assert_live(&mut inspect).await?;
        }
        let registered = registered_fit(&mut inspect, request.source_id).await?;
        let prepared = match &registered {
            Ok(_) => read_input(&mut inspect, request, archive, cancelled.clone()).await,
            Err(skip) => Ok(PreparedReplay::Skip(*skip)),
        };
        inspect.commit().await?;

        let (mut txn, candidate) = match prepared {
            Ok(PreparedReplay::Ready(input)) => {
                let ReplayContent::Fit(file) = input.content else {
                    return Err(FitReplayError::OtherEndpoint);
                };
                let bytes = file
                    .metadata()
                    .map_err(|error| FitIngestError::Archive(error.into()))?
                    .len();
                let key = input.source.archive_key;
                if bytes == 0 {
                    (
                        self.work.begin().await?,
                        Candidate::Verified {
                            key,
                            bytes,
                            projection: Projection::Empty,
                        },
                    )
                } else {
                    match self
                        .stage_fit_projection_cancellable(file, limits, cancelled.clone())
                        .await
                    {
                        Ok(staged) => {
                            let messages = staged.inventory().messages;
                            (
                                staged.transaction,
                                Candidate::Verified {
                                    key,
                                    bytes,
                                    projection: Projection::Staged(messages),
                                },
                            )
                        }
                        Err(FitStageError::Decode(FitError::Cancelled)) => {
                            return Err(FitReplayError::Cancelled);
                        }
                        Err(FitStageError::Decode(error)) if permanent_decode_error(error) => (
                            self.work.begin().await?,
                            Candidate::Verified {
                                key,
                                bytes,
                                projection: Projection::DecodeError(legacy_decode_label(error)),
                            },
                        ),
                        Err(error) => return Err(FitIngestError::Stage(error).into()),
                    }
                }
            }
            Ok(PreparedReplay::Skip(ReplaySkip::Unchanged)) => (
                self.work.begin().await?,
                Candidate::Verified {
                    key: registered.map_err(|_| FitReplayError::SourceChanged)?.0,
                    bytes: 0,
                    projection: Projection::Current,
                },
            ),
            Ok(PreparedReplay::Skip(_)) => (self.work.begin().await?, Candidate::Skip),
            Err(ReplayInputError::Archive(ArchiveError::ReadCancelled)) => {
                return Err(FitReplayError::Cancelled);
            }
            Err(error) => {
                let reason = archive_failure(error)?;
                let (key, hash) = registered.map_err(|_| FitReplayError::SourceChanged)?;
                (
                    self.work.begin().await?,
                    Candidate::Rejected { key, hash, reason },
                )
            }
        };
        authorize_replay(&mut txn, request.authenticated_fingerprint).await?;
        if let Some(lease) = lease {
            lease.assert_live(&mut txn).await?;
        }
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(REPLAY_MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !locked {
            return Err(FitReplayError::Busy);
        }
        if cancelled() {
            return Err(FitReplayError::Cancelled);
        }
        let publish_ms = limits.max_duration.as_millis().clamp(750, i32::MAX as u128);
        sqlx::query("SELECT set_config('statement_timeout', $1, true)")
            .bind(format!("{publish_ms}ms"))
            .execute(&mut *txn)
            .await?;
        let outcome = finish_candidate(&mut txn, request, candidate).await?;
        if cancelled() {
            return Err(FitReplayError::Cancelled);
        }
        store_replay_outcome(&mut txn, request.source_id, outcome).await?;
        let job = if let Some(lease) = lease {
            let status = if matches!(
                outcome,
                FitReplayOutcome::RejectedArchive(_)
                    | FitReplayOutcome::Applied(FitIngestOutcome::Error { .. })
            ) {
                JobOutcome::Failed {
                    reason: FailureReason::RuntimeError,
                    retry_at: None,
                }
            } else {
                JobOutcome::Completed
            };
            Some(lease.finish_in(&mut txn, status).await?)
        } else {
            None
        };
        txn.commit().await?;
        Ok((outcome, job))
    }
}

async fn registered_fit(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
) -> Result<Result<(String, String), ReplaySkip>, FitReplayError> {
    let row: Option<(String, String, String, String)> = sqlx::query_as(
        "SELECT source,endpoint,archive_key,payload_hash FROM source_payloads WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(&mut **txn)
    .await?;
    match row {
        None => Ok(Err(ReplaySkip::SourceRemoved)),
        Some((source, ..)) if source != GARMIN_LEGACY_SOURCE => {
            Ok(Err(ReplaySkip::UnsupportedSource))
        }
        Some((_, endpoint, ..)) if endpoint != ENDPOINT => Err(FitReplayError::OtherEndpoint),
        Some((_, _, key, hash)) => Ok(Ok((key, hash))),
    }
}

async fn finish_candidate(
    txn: &mut Transaction<'_, Postgres>,
    request: ReadReplayInput<'_>,
    candidate: Candidate,
) -> Result<FitReplayOutcome, FitReplayError> {
    let (key, hash) = match registered_fit(txn, request.source_id).await? {
        Ok(key) => key,
        Err(skip) => return Ok(FitReplayOutcome::Skipped(skip)),
    };
    let canonical = select_sources(txn, PARSER_VERSION, None, Some(&[request.source_id]), true)
        .await
        .map_err(ReplayInputError::Access)?
        .sources
        .pop()
        .filter(|source| source.canonical);
    let Some(source) = canonical else {
        return Ok(FitReplayOutcome::Skipped(ReplaySkip::SupersededRevision));
    };
    match candidate {
        Candidate::Skip => Err(FitReplayError::SourceChanged),
        Candidate::Rejected {
            key: expected_key,
            hash: expected_hash,
            reason,
        } => {
            if key != expected_key || hash != expected_hash {
                return Err(FitReplayError::SourceChanged);
            }
            Ok(FitReplayOutcome::RejectedArchive(reason))
        }
        Candidate::Verified {
            key: verified,
            bytes,
            projection,
        } => {
            let key = key
                .parse::<ga_archive::ArchiveKey>()
                .map_err(|_| ReplayInputError::InvalidArchiveKey)?;
            if verified != key.to_string() || key.digest_hex() != hash {
                return Err(FitReplayError::SourceChanged);
            }
            if source.previous_parser_version == PARSER_VERSION
                && matches!(source.status.as_str(), "normalized" | "empty")
            {
                return Ok(FitReplayOutcome::Skipped(ReplaySkip::Unchanged));
            }
            let capture = CapturedFit {
                id: source.id,
                key,
                requested_at: fit_clock(txn, &source).await?,
                activity: source.source_key,
                owner: request.authenticated_fingerprint.to_owned(),
                bytes,
            };
            apply_verified(txn, &capture, projection).await
        }
    }
}

async fn apply_verified(
    txn: &mut Transaction<'_, Postgres>,
    capture: &CapturedFit,
    projection: Projection,
) -> Result<FitReplayOutcome, FitReplayError> {
    lock_legacy_scope(txn, &capture.state_key())
        .await
        .map_err(FitIngestError::Access)?;
    let current = match LockedFit::load(txn, capture).await {
        Err(FitIngestError::ActivityMissing) => {
            return Ok(FitReplayOutcome::RejectedArchive("LookupError"));
        }
        other => other?,
    };
    let source = capture.id.to_string();
    let attempt = current
        .state
        .get("latest_attempt")
        .and_then(Value::as_object);
    let mode = ReplayWatermark {
        replay: true,
        preserve_attempt: attempt.is_some_and(|attempt| {
            !attempt.is_empty()
                && attempt.get("source_ref").and_then(Value::as_str) != Some(&source)
        }),
        retained: current.activity_parsed_archive
            && current
                .latest_requested()?
                .is_some_and(|at| at > capture.requested_at),
    };
    if matches!(projection, Projection::Current) {
        return Err(FitReplayError::SourceChanged);
    }
    let outcome = if let Some(outcome) = current.short_circuit_with(txn, capture, mode).await? {
        outcome
    } else {
        match projection {
            Projection::Staged(rows) => {
                let mut replacement = txn.begin().await?;
                if replace_fit_rows(&mut replacement, capture, rows)
                    .await
                    .is_err()
                {
                    replacement.rollback().await?;
                    current
                        .record_error_with(txn, capture, "DatabaseError", mode)
                        .await?
                } else {
                    replacement.commit().await?;
                    current.record_success_with(txn, capture, mode).await?;
                    FitIngestOutcome::Normalized { rows }
                }
            }
            Projection::DecodeError(reason) => {
                current
                    .record_error_with(txn, capture, reason, mode)
                    .await?
            }
            Projection::Current | Projection::Empty => return Err(FitReplayError::SourceChanged),
        }
    };
    if matches!(
        outcome,
        FitIngestOutcome::Normalized { .. } | FitIngestOutcome::Empty
    ) {
        invalidate_replay_outputs(txn)
            .await
            .map_err(|error| match error {
                DocumentError::Access(CaptureError::Busy) => FitReplayError::Busy,
                _ => FitReplayError::Database,
            })?;
    }
    Ok(FitReplayOutcome::Applied(outcome))
}

fn archive_failure(error: ReplayInputError) -> Result<&'static str, ReplayInputError> {
    match error {
        ReplayInputError::Archive(ArchiveError::Io(ref error))
            if error.kind() == std::io::ErrorKind::NotFound =>
        {
            Ok("FileNotFoundError")
        }
        ReplayInputError::Archive(
            ArchiveError::InvalidObject
            | ArchiveError::ExistingObjectDigestMismatch
            | ArchiveError::PayloadTooLarge,
        )
        | ReplayInputError::InvalidArchiveKey
        | ReplayInputError::ArchiveHashMismatch
        | ReplayInputError::HistoricalClock => Ok("ValueError"),
        error => Err(error),
    }
}

async fn store_replay_outcome(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    outcome: FitReplayOutcome,
) -> Result<(), FitReplayError> {
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let at = StoredInstant::try_from(at).map_err(|_| FitReplayError::Database)?;
    let reference = id.to_string();
    let value = match outcome {
        FitReplayOutcome::Skipped(ReplaySkip::SourceRemoved) => json!({"status": "source_removed"}),
        FitReplayOutcome::Skipped(ReplaySkip::UnsupportedSource) => {
            json!({"status": "unsupported_source"})
        }
        FitReplayOutcome::Skipped(ReplaySkip::SupersededRevision) => {
            json!({"status": "superseded_revision"})
        }
        FitReplayOutcome::Skipped(ReplaySkip::Unchanged)
        | FitReplayOutcome::Applied(FitIngestOutcome::Unchanged) => {
            json!({"status": "unchanged", "source_ref": reference})
        }
        FitReplayOutcome::RejectedArchive(reason) => {
            json!({"status": "error", "error_type": reason})
        }
        FitReplayOutcome::Applied(FitIngestOutcome::Normalized { rows }) => {
            json!({"status": "normalized", "source_ref": reference, "rows": rows})
        }
        FitReplayOutcome::Applied(FitIngestOutcome::Empty) => {
            json!({"status": "empty", "source_ref": reference, "rows": 0})
        }
        FitReplayOutcome::Applied(FitIngestOutcome::Stale) => {
            json!({"status": "stale", "source_ref": reference, "rows": 0})
        }
        FitReplayOutcome::Applied(FitIngestOutcome::Error { reason }) => {
            json!({"status": "error", "source_ref": reference, "error_type": reason})
        }
    };
    let mut value = value;
    value["target_version"] = json!(PARSER_VERSION);
    value["at"] = json!(at.to_string());
    put_state(txn, &format!("replay:{id}:{PARSER_VERSION}"), &value).await?;
    Ok(())
}
