mod historical;

use std::{
    error::Error,
    fmt,
    fs::File,
    io::{BufReader, Read},
    sync::Arc,
};

use ga_archive::{Archive, ArchiveError, ArchiveKey};
use ga_domain::timestamp::StoredInstant;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{ReplaySelectionError, ReplaySource, select_sources};
use crate::{
    Database, check_readiness, garmin_owner::verify_owner_in_transaction,
    raw_capture::GARMIN_LEGACY_SOURCE,
};

pub const GARMIN_PARSER_VERSION: i32 = crate::activity_documents::PARSER_VERSION;

#[derive(Clone, Copy)]
pub struct ReadReplayInput<'a> {
    pub authenticated_fingerprint: &'a str,
    pub source_id: Uuid,
    pub target_parser_version: i32,
    pub configured_timezone: &'a str,
    pub max_json_bytes: u64,
    pub max_fit_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplaySkip {
    SourceRemoved,
    UnsupportedSource,
    SupersededRevision,
    Unchanged,
}

pub enum PreparedReplay {
    Skip(ReplaySkip),
    Ready(Box<ReplayInput>),
}

pub struct ReplayInput {
    pub source: ReplaySource,
    pub requested_at: StoredInstant,
    pub content: ReplayContent,
}

pub enum ReplayContent {
    Fit(File),
    Json {
        payload: Value,
        timezone: String,
        replacement: Option<ReplayReplacement>,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReplayReplacement {
    pub start: StoredInstant,
    pub end: StoredInstant,
    pub metrics: Vec<String>,
    pub evidence: String,
}

#[derive(Debug)]
pub enum ReplayInputError {
    Access(ReplaySelectionError),
    NewerParserRequired,
    MatchingParserRequired,
    InvalidArchiveKey,
    ArchiveHashMismatch,
    Archive(ArchiveError),
    ArchiveTask,
    InvalidPayload,
    HistoricalClock,
    HistoricalTimezone,
    HistoricalContract,
    Database,
}

impl fmt::Display for ReplayInputError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Access(_) => "Replay input could not be authorized",
            Self::NewerParserRequired => "Replay requires a newer parser version",
            Self::MatchingParserRequired => "Replay requires its matching parser version",
            Self::InvalidArchiveKey => "Archived source key is invalid",
            Self::ArchiveHashMismatch => "Archived source hash mismatch",
            Self::Archive(_) => "Replay source archive could not be verified",
            Self::ArchiveTask => "Replay archive verification ended unexpectedly",
            Self::InvalidPayload => "Archived replay source payload is invalid",
            Self::HistoricalClock => "Historical application time is unavailable",
            Self::HistoricalTimezone => "Historical interpretation timezone is unavailable",
            Self::HistoricalContract => "Retained projection application contract is unavailable",
            Self::Database => "Replay source context could not be read",
        })
    }
}
impl Error for ReplayInputError {}

impl From<sqlx::Error> for ReplayInputError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Read and verify archived input without fetching from Garmin or applying a projection.
    /// A writer must recheck the owner, canonical revision and context under its mutation fence.
    pub async fn read_replay_input(
        &self,
        request: ReadReplayInput<'_>,
        archive: Arc<Archive>,
        cancelled: impl Fn() -> bool + Send + 'static,
    ) -> Result<PreparedReplay, ReplayInputError> {
        require_parser(request.target_parser_version)?;
        let mut txn = self
            .work
            .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await?;
        authorize_replay(&mut txn, request.authenticated_fingerprint).await?;
        let input = read_input(&mut txn, request, archive, cancelled).await;
        txn.rollback().await?;
        input
    }
}

pub(super) fn require_parser(target: i32) -> Result<(), ReplayInputError> {
    match target.cmp(&GARMIN_PARSER_VERSION) {
        std::cmp::Ordering::Greater => Err(ReplayInputError::NewerParserRequired),
        std::cmp::Ordering::Less => Err(ReplayInputError::MatchingParserRequired),
        std::cmp::Ordering::Equal => Ok(()),
    }
}

pub(super) async fn authorize_replay(
    txn: &mut Transaction<'_, Postgres>,
    authenticated_fingerprint: &str,
) -> Result<(), ReplayInputError> {
    sqlx::query("SET LOCAL statement_timeout = '750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn)
        .await
        .map_err(ReplaySelectionError::Unavailable)
        .map_err(ReplayInputError::Access)?;
    verify_owner_in_transaction(txn, authenticated_fingerprint)
        .await
        .map_err(ReplaySelectionError::Owner)
        .map_err(ReplayInputError::Access)?;
    Ok(())
}

pub(super) async fn read_input(
    txn: &mut Transaction<'_, Postgres>,
    request: ReadReplayInput<'_>,
    archive: Arc<Archive>,
    cancelled: impl Fn() -> bool + Send + 'static,
) -> Result<PreparedReplay, ReplayInputError> {
    let id = request.source_id;
    let Some((source, hash)) = sqlx::query_as::<_, (String, String)>(
        "SELECT source, payload_hash FROM source_payloads WHERE id = $1",
    )
    .bind(id)
    .fetch_optional(&mut **txn)
    .await?
    else {
        return Ok(PreparedReplay::Skip(ReplaySkip::SourceRemoved));
    };
    if source != GARMIN_LEGACY_SOURCE {
        return Ok(PreparedReplay::Skip(ReplaySkip::UnsupportedSource));
    }
    let Some(source) = select_sources(txn, GARMIN_PARSER_VERSION, None, Some(&[id]), true)
        .await
        .map_err(ReplayInputError::Access)?
        .sources
        .pop()
        .filter(|source| source.canonical)
    else {
        return Ok(PreparedReplay::Skip(ReplaySkip::SupersededRevision));
    };

    let fit = source.endpoint == "activity_fit";
    let unchanged = fit
        && source.previous_parser_version == GARMIN_PARSER_VERSION
        && matches!(source.status.as_str(), "normalized" | "empty");
    let key: ArchiveKey = source
        .archive_key
        .parse()
        .map_err(|_| ReplayInputError::InvalidArchiveKey)?;
    if key.digest_hex() != hash {
        return Err(ReplayInputError::ArchiveHashMismatch);
    }
    let limit = if fit {
        request.max_fit_bytes
    } else {
        request.max_json_bytes
    };
    let archived = tokio::task::spawn_blocking(move || {
        let file = archive
            .open_verified_reader(&key, limit, cancelled)
            .map_err(ReplayInputError::Archive)?;
        if fit {
            Ok(Archived::Fit(file))
        } else {
            serde_json::from_reader(BufReader::new(file.take(limit.saturating_add(1))))
                .map(Archived::Json)
                .map_err(|_| ReplayInputError::InvalidPayload)
        }
    })
    .await
    .map_err(|_| ReplayInputError::ArchiveTask)??;

    if unchanged {
        return Ok(PreparedReplay::Skip(ReplaySkip::Unchanged));
    }
    let (requested_at, content) = match archived {
        Archived::Fit(file) => (
            historical::fit_clock(txn, &source).await?,
            ReplayContent::Fit(file),
        ),
        Archived::Json(payload) => {
            let (at, timezone, replacement) = historical::json_context(
                txn,
                &source,
                &payload,
                request.configured_timezone,
                request.max_json_bytes,
            )
            .await?;
            (
                at,
                ReplayContent::Json {
                    payload,
                    timezone,
                    replacement,
                },
            )
        }
    };
    Ok(PreparedReplay::Ready(Box::new(ReplayInput {
        source,
        requested_at,
        content,
    })))
}

enum Archived {
    Fit(File),
    Json(Value),
}
