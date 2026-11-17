use std::{error::Error, fmt, future::Future, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
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
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ShareCategory {
    Schema,
    Facts,
    OriginalText,
}

#[derive(Clone, Copy)]
pub struct ShareRequirement<'a> {
    pub version_id: Uuid,
    pub categories: &'a [ShareCategory],
}

#[derive(Clone, Copy, Debug)]
pub enum SharePolicyError {
    InvalidDestination,
    Storage(ReadinessError),
    Busy,
    Database,
}

impl fmt::Display for SharePolicyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidDestination => "Invalid tracker sharing destination",
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

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Consent {
    definition_id: String,
    destination_kind: DestinationKind,
    destination_instance_id: String,
    categories: Vec<ShareCategory>,
    granted_at: String,
    #[serde(default = "initial_revision")]
    policy_revision: u8,
}

const fn initial_revision() -> u8 {
    1
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
    type Row = (Uuid, String, Option<String>, OffsetDateTime);
    let row = sqlx::query_as::<_, Row>(
        "SELECT version.definition_id, version.privacy, consent.value::text, clock_timestamp()
         FROM event_definition_versions AS version
         LEFT JOIN app_state AS consent
           ON consent.key = concat('tracker-consent:', version.definition_id, ':', $2::text, ':', $3::text)
         WHERE version.id=$1",
    )
    .bind(requirement.version_id)
    .bind(destination.kind.label())
    .bind(destination.instance)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((definition, privacy, saved, checked_at)) = row else {
        return Ok(false);
    };
    if privacy == "private"
        && !requirement
            .categories
            .contains(&ShareCategory::OriginalText)
    {
        return Ok(true);
    }
    let Some(consent) = saved.and_then(|value| serde_json::from_str::<Consent>(&value).ok()) else {
        return Ok(false);
    };
    let valid_time = consent
        .granted_at
        .parse::<StoredInstant>()
        .is_ok_and(|at| OffsetDateTime::from(at) <= checked_at);
    Ok(
        consent.definition_id.parse::<Uuid>().ok() == Some(definition)
            && consent.destination_kind == destination.kind
            && consent.destination_instance_id == destination.instance
            && consent.policy_revision == 1
            && valid_time
            && requirement
                .categories
                .iter()
                .all(|category| consent.categories.contains(category)),
    )
}
