mod calendar;
mod input;
mod queue;
mod status;

pub use calendar::{CalendarReplayError, CalendarReplayOutcome};
pub use input::{
    GARMIN_PARSER_VERSION, PreparedReplay, ReadReplayInput, ReplayContent, ReplayInput,
    ReplayInputError, ReplayReplacement, ReplaySkip,
};
pub use queue::{ReplaySchedule, ScheduleReplay};
pub use status::ReplayStatus;
pub(crate) use status::{REPLAY_MUTATION_LOCK, projection_pending};

use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness,
    garmin_owner::{OwnerVerificationError, verify_owner_in_transaction},
};

const SCAN_LIMIT: i64 = 128;

pub struct ReplaySelection<'a> {
    pub authenticated_fingerprint: &'a str,
    pub target_parser_version: i32,
    pub after: Option<Uuid>,
}

pub struct ReplaySource {
    pub id: Uuid,
    pub endpoint: String,
    pub source_key: String,
    pub previous_parser_version: i32,
    pub status: String,
    pub archive_key: String,
    pub fetched_at: StoredInstant,
    pub canonical: bool,
}

pub struct ReplayPage {
    pub sources: Vec<ReplaySource>,
    pub next_after: Option<Uuid>,
}

#[derive(Debug)]
pub enum ReplaySelectionError {
    Unavailable(ReadinessError),
    Owner(OwnerVerificationError),
    InvalidVersion,
    InvalidClock,
    Database,
}

impl fmt::Display for ReplaySelectionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Unavailable(_) => "Replay inventory storage is unavailable",
            Self::Owner(_) => "Replay inventory owner could not be verified",
            Self::InvalidVersion => "Replay target parser version is invalid",
            Self::InvalidClock => "Archived source has an invalid fetch time",
            Self::Database => "Replay inventory could not be read",
        })
    }
}
impl Error for ReplaySelectionError {}

impl From<sqlx::Error> for ReplaySelectionError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Read one bounded slice of archived sources for an upgrade or rollback.
    pub async fn replay_source_page(
        &self,
        selection: ReplaySelection<'_>,
    ) -> Result<ReplayPage, ReplaySelectionError> {
        if selection.target_parser_version < 1 {
            return Err(ReplaySelectionError::InvalidVersion);
        }
        tokio::time::timeout(
            Duration::from_millis(1500),
            self.replay_source_transaction(selection),
        )
        .await
        .map_err(|_| ReplaySelectionError::Database)?
    }

    async fn replay_source_transaction(
        &self,
        selection: ReplaySelection<'_>,
    ) -> Result<ReplayPage, ReplaySelectionError> {
        let mut txn = self.work.begin_with("BEGIN READ ONLY").await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(ReplaySelectionError::Unavailable)?;
        verify_owner_in_transaction(&mut txn, selection.authenticated_fingerprint)
            .await
            .map_err(ReplaySelectionError::Owner)?;
        let page = source_page(
            &mut txn,
            selection.target_parser_version,
            selection.after,
            None,
        )
        .await?;
        txn.rollback().await?;
        Ok(page)
    }
}

async fn source_page(
    txn: &mut Transaction<'_, Postgres>,
    target: i32,
    after: Option<Uuid>,
    ids: Option<&[Uuid]>,
) -> Result<ReplayPage, ReplaySelectionError> {
    select_sources(txn, target, after, ids, false).await
}

async fn select_sources(
    txn: &mut Transaction<'_, Postgres>,
    target: i32,
    after: Option<Uuid>,
    ids: Option<&[Uuid]>,
    include_current: bool,
) -> Result<ReplayPage, ReplaySelectionError> {
    type SourceRow = (
        Uuid,
        String,
        String,
        i32,
        String,
        String,
        OffsetDateTime,
        bool,
    );
    let rows = sqlx::query_as::<_, SourceRow>(include_str!("replay/selection.sql"))
        .bind(target)
        .bind(after)
        .bind(SCAN_LIMIT)
        .bind(ids)
        .bind(include_current)
        .fetch_all(&mut **txn)
        .await?;
    let next_after = if rows.len() as i64 == SCAN_LIMIT {
        rows.last().map(|row| row.0)
    } else {
        None
    };
    let sources = rows
        .into_iter()
        .map(
            |(
                id,
                endpoint,
                source_key,
                previous_parser_version,
                status,
                archive_key,
                fetched,
                canonical,
            )| {
                Ok(ReplaySource {
                    id,
                    endpoint,
                    source_key,
                    previous_parser_version,
                    status,
                    archive_key,
                    canonical,
                    fetched_at: fetched
                        .try_into()
                        .map_err(|_| ReplaySelectionError::InvalidClock)?,
                })
            },
        )
        .collect::<Result<_, ReplaySelectionError>>()?;
    Ok(ReplayPage {
        sources,
        next_after,
    })
}
