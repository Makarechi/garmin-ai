mod consent_records;
mod consent_writes;

pub use consent_records::TrackerShareConsent;
pub use consent_writes::TrackerShareGrant;

use std::{error::Error, fmt, future::Future, time::Duration};

use serde::Deserialize;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness, replay::REPLAY_MUTATION_LOCK};

const CHANNEL_CONSENT_LOCK: i64 = 72104631;
const MODEL_CONSENT_LOCK: i64 = 72104632;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum DestinationKind {
    Model,
    Channel,
}

impl DestinationKind {
    fn label(self) -> &'static str {
        match self {
            Self::Model => "model",
            Self::Channel => "channel",
        }
    }

    fn lock(self) -> i64 {
        match self {
            Self::Model => MODEL_CONSENT_LOCK,
            Self::Channel => CHANNEL_CONSENT_LOCK,
        }
    }
}

#[derive(Clone, Copy)]
pub struct ShareDestination<'a> {
    kind: DestinationKind,
    instance: &'a str,
}

impl<'a> ShareDestination<'a> {
    pub fn new(kind: DestinationKind, instance: &'a str) -> Result<Self, SharePolicyError> {
        let mut bytes = instance.bytes();
        if instance.len() > 200
            || !bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
            || !bytes.all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.:-".contains(&byte)
            })
        {
            return Err(SharePolicyError::InvalidDestination);
        }
        Ok(Self { kind, instance })
    }

    pub(crate) fn kind(self) -> DestinationKind {
        self.kind
    }

    pub(crate) fn instance(self) -> &'a str {
        self.instance
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ShareCategory {
    Schema,
    Facts,
    OriginalText,
}

impl ShareCategory {
    fn label(self) -> &'static str {
        match self {
            Self::Schema => "schema",
            Self::Facts => "facts",
            Self::OriginalText => "original_text",
        }
    }
}

#[derive(Clone, Copy)]
pub struct ShareRequirement<'a> {
    pub version_id: Uuid,
    pub categories: &'a [ShareCategory],
}

#[derive(Clone, Copy, Debug)]
pub enum SharePolicyError {
    InvalidDestination,
    InvalidConsent,
    Forbidden,
    DefinitionMissing,
    Storage(ReadinessError),
    Busy,
    Database,
}

impl fmt::Display for SharePolicyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidDestination => "Invalid tracker sharing destination",
            Self::InvalidConsent => "Tracker sharing consent is invalid",
            Self::Forbidden => "Integration consent management permission required",
            Self::DefinitionMissing => "Tracker definition not found",
            Self::Storage(_) => "Tracker sharing storage is unavailable",
            Self::Busy => "Tracker sharing consent is being updated",
            Self::Database => "Tracker sharing consent could not be verified",
        })
    }
}
impl Error for SharePolicyError {}

impl From<sqlx::Error> for SharePolicyError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Check the privacy of the exact tracker version and the current consent for this recipient.
    pub async fn tracker_version_allowed(
        &self,
        requirement: ShareRequirement<'_>,
        destination: ShareDestination<'_>,
    ) -> Result<bool, SharePolicyError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_share_check().await?;
            let permitted = allows(&mut txn, requirement, destination).await?;
            txn.commit().await?;
            Ok(permitted)
        })
        .await
        .map_err(|_| SharePolicyError::Database)?
    }

    /// Check every recorded sharing dependency before starting delivery. A concurrent
    /// consent mutation waits until the operation ends or this future is dropped.
    pub async fn with_tracker_delivery<T, F>(
        &self,
        destination: ShareDestination<'_>,
        requirements: &[ShareRequirement<'_>],
        deliver: impl FnOnce() -> F,
    ) -> Result<Option<T>, SharePolicyError>
    where
        F: Future<Output = T>,
    {
        let txn = tokio::time::timeout(
            Duration::from_millis(1500),
            self.prepare_tracker_delivery(destination, requirements),
        )
        .await
        .map_err(|_| SharePolicyError::Database)??;
        let Some(txn) = txn else {
            return Ok(None);
        };
        let result = deliver().await;
        // No database changes follow delivery. Dropping the read transaction
        // releases the lock without turning a completed send into a retry.
        drop(txn);
        Ok(Some(result))
    }

    async fn begin_share_check(&self) -> Result<Transaction<'static, Postgres>, SharePolicyError> {
        let mut txn = self.work.begin_with("BEGIN READ ONLY").await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(SharePolicyError::Storage)?;
        Ok(txn)
    }

    async fn prepare_tracker_delivery(
        &self,
        destination: ShareDestination<'_>,
        requirements: &[ShareRequirement<'_>],
    ) -> Result<Option<Transaction<'static, Postgres>>, SharePolicyError> {
        let mut txn = self.begin_share_check().await?;
        if destination.kind == DestinationKind::Channel {
            try_shared_lock(&mut txn, REPLAY_MUTATION_LOCK).await?;
        }
        try_shared_lock(&mut txn, destination.kind.lock()).await?;
        for requirement in requirements {
            if !allows(&mut txn, *requirement, destination).await? {
                txn.rollback().await?;
                return Ok(None);
            }
        }
        Ok(Some(txn))
    }
}

async fn try_shared_lock(
    txn: &mut Transaction<'_, Postgres>,
    key: i64,
) -> Result<(), SharePolicyError> {
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
        .bind(key)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(SharePolicyError::Busy);
    }
    Ok(())
}

async fn allows(
    txn: &mut Transaction<'_, Postgres>,
    requirement: ShareRequirement<'_>,
    destination: ShareDestination<'_>,
) -> Result<bool, SharePolicyError> {
    if requirement.categories.is_empty() {
        return Ok(false);
    }
    let categories: Vec<_> = requirement
        .categories
        .iter()
        .map(|part| part.label())
        .collect();
    sqlx::query_scalar(concat!(
        include_str!("share_policy/version.sql"),
        include_str!("share_policy/consent.sql"),
        ")) )"
    ))
    .bind(requirement.version_id)
    .bind(destination.kind.label())
    .bind(destination.instance)
    .bind(categories)
    .fetch_one(&mut **txn)
    .await
    .map_err(Into::into)
}
