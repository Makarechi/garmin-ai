use std::time::Duration;

use ga_domain::{
    access::{Scope, Scopes},
    timestamp::StoredInstant,
};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    DestinationKind, ShareCategory, ShareDestination, SharePolicyError, TrackerShareConsent,
    consent_records::consent_document, try_shared_lock,
};
use crate::{Database, check_readiness, replay::REPLAY_MUTATION_LOCK};

const DELIVERY_POLICY_LOCK: i64 = 72_104_621;
const CONTEXT_DELIVERY_LOCK: i64 = 72_104_643;
const PRIMARY_CHANNEL: &str = "telegram:primary";
const PENDING_KEY: &str = "analysis:conversation:pending";

pub struct TrackerShareGrant<'a> {
    pub definition_id: Uuid,
    pub destination: ShareDestination<'a>,
    pub categories: &'a [ShareCategory],
    pub granted_at: StoredInstant,
}

impl Database {
    /// Grant or narrow a tracker consent and retire any affected derived context atomically.
    pub async fn grant_tracker_share(
        &self,
        scopes: Scopes,
        grant: TrackerShareGrant<'_>,
    ) -> Result<(), SharePolicyError> {
        let granted_at = grant.granted_at.to_string().replace("+00:00", "Z");
        self.write_tracker_share(scopes, grant, &granted_at).await
    }

    pub async fn store_tracker_share(
        &self,
        scopes: Scopes,
        consent: &TrackerShareConsent,
    ) -> Result<(), SharePolicyError> {
        self.write_tracker_share(
            scopes,
            TrackerShareGrant {
                definition_id: consent.definition_id,
                destination: consent.destination(),
                categories: &consent.categories,
                granted_at: consent.granted_at,
            },
            &consent.granted_at_json,
        )
        .await
    }

    async fn write_tracker_share(
        &self,
        scopes: Scopes,
        grant: TrackerShareGrant<'_>,
        granted_at: &str,
    ) -> Result<(), SharePolicyError> {
        require_permission(scopes)?;
        if grant.categories.is_empty() {
            return Err(SharePolicyError::InvalidConsent);
        }
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_consent_write(grant.destination).await?;
            let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            if OffsetDateTime::from(grant.granted_at) > now {
                return Err(SharePolicyError::InvalidConsent);
            }
            let exists: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM event_definitions WHERE id=$1 AND namespace='user')",
            )
            .bind(grant.definition_id)
            .fetch_one(&mut *txn)
            .await?;
            if !exists {
                return Err(SharePolicyError::DefinitionMissing);
            }
            let key = consent_key(grant.definition_id, grant.destination);
            let previous: Option<String> =
                sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                    .bind(&key)
                    .fetch_optional(&mut *txn)
                    .await?;
            if let Some(previous) = previous {
                let record = TrackerShareConsent::parse(previous.as_bytes())?;
                if !record.matches(grant.definition_id, grant.destination) {
                    return Err(SharePolicyError::InvalidConsent);
                }
                let retire = |important: &[ShareCategory]| {
                    important.iter().any(|category| {
                        record.categories.contains(category) && !grant.categories.contains(category)
                    })
                };
                let affects_context = match grant.destination.kind() {
                    DestinationKind::Model => {
                        retire(&[ShareCategory::Facts, ShareCategory::OriginalText])
                    }
                    DestinationKind::Channel => {
                        retire(&[ShareCategory::Schema, ShareCategory::Facts])
                    }
                };
                if affects_context {
                    forget_context(&mut txn, grant.definition_id, grant.destination).await?;
                }
            }
            let record = consent_document(
                grant.definition_id,
                grant.destination,
                grant.categories,
                granted_at,
            );
            sqlx::query(
                "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
            )
            .bind(key)
            .bind(record.to_string())
            .execute(&mut *txn)
            .await?;
            txn.commit().await?;
            Ok(())
        })
        .await
        .map_err(|_| SharePolicyError::Database)?
    }

    /// Revoke the exact destination consent and retire its dependent context.
    pub async fn revoke_tracker_share(
        &self,
        scopes: Scopes,
        definition_id: Uuid,
        destination: ShareDestination<'_>,
    ) -> Result<bool, SharePolicyError> {
        require_permission(scopes)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_consent_write(destination).await?;
            let deleted = sqlx::query("DELETE FROM app_state WHERE key=$1")
                .bind(consent_key(definition_id, destination))
                .execute(&mut *txn)
                .await?
                .rows_affected()
                != 0;
            if deleted {
                forget_context(&mut txn, definition_id, destination).await?;
            }
            txn.commit().await?;
            Ok(deleted)
        })
        .await
        .map_err(|_| SharePolicyError::Database)?
    }

    async fn begin_consent_write(
        &self,
        destination: ShareDestination<'_>,
    ) -> Result<Transaction<'static, Postgres>, SharePolicyError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(SharePolicyError::Storage)?;
        // The same lock order is used by Python and by queued delivery.
        try_exclusive_lock(&mut txn, REPLAY_MUTATION_LOCK).await?;
        try_exclusive_lock(&mut txn, destination.kind().lock()).await?;
        Ok(txn)
    }
}

pub(super) fn require_permission(scopes: Scopes) -> Result<(), SharePolicyError> {
    if scopes.permits(&[Scope::ManageIntegrations]) {
        Ok(())
    } else {
        Err(SharePolicyError::Forbidden)
    }
}

pub(super) fn consent_key(definition_id: Uuid, destination: ShareDestination<'_>) -> String {
    format!(
        "tracker-consent:{definition_id}:{}:{}",
        destination.kind().label(),
        destination.instance()
    )
}

async fn try_exclusive_lock(
    txn: &mut Transaction<'_, Postgres>,
    id: i64,
) -> Result<(), SharePolicyError> {
    let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(id)
        .fetch_one(&mut **txn)
        .await?;
    if acquired {
        Ok(())
    } else {
        Err(SharePolicyError::Busy)
    }
}

async fn forget_context(
    txn: &mut Transaction<'_, Postgres>,
    definition: Uuid,
    destination: ShareDestination<'_>,
) -> Result<(), SharePolicyError> {
    try_shared_lock(txn, DELIVERY_POLICY_LOCK).await?;
    try_exclusive_lock(txn, CONTEXT_DELIVERY_LOCK).await?;
    match destination.kind() {
        DestinationKind::Model => {
            sqlx::query(include_str!("forget_model.sql"))
                .execute(&mut **txn)
                .await?;
            sqlx::query(
                "UPDATE conversations SET state=state-'analysis_turns',
                    memory_epoch=gen_random_uuid(),updated_at=now()",
            )
            .execute(&mut **txn)
            .await?;
        }
        DestinationKind::Channel => {
            let instance = destination.instance();
            sqlx::query(
                "UPDATE outbox_messages SET state='cancelled',next_attempt_at=NULL,updated_at=now()
                 WHERE state='queued'
                   AND concat(intent->'channel_instance'->>'channel',':',
                              intent->'channel_instance'->>'instance_id')=$1
                   AND intent->'evidence_refs' ? $2",
            )
            .bind(instance)
            .bind(format!("definition:{definition}"))
            .execute(&mut **txn)
            .await?;
            let pending_key = if instance == PRIMARY_CHANNEL {
                PENDING_KEY.to_owned()
            } else {
                format!("{PENDING_KEY}:{instance}")
            };
            sqlx::query(include_str!("forget_channel.sql"))
                .bind(instance)
                .bind(pending_key)
                .execute(&mut **txn)
                .await?;
        }
    }
    Ok(())
}
