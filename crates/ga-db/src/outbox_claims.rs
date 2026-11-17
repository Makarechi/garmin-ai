use std::{collections::BTreeMap, error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use sqlx::{Postgres, Row, Transaction, postgres::PgRow};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness, replay::REPLAY_MUTATION_LOCK};

const CANDIDATES_PER_CLAIM: usize = 64;

pub struct ReplyDestination<'a> {
    pub owner_id: Uuid,
    pub channel: &'a str,
    pub instance_id: &'a str,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub struct ReplyClaim {
    pub outbox_id: Uuid,
    pub lease_token: Uuid,
    pub lease_until: StoredInstant,
}

#[derive(Debug)]
pub enum ReplyClaimError {
    InvalidRequest,
    Storage(ReadinessError),
    Busy,
    Database,
}

impl fmt::Display for ReplyClaimError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidRequest => "Outbox claim parameters are invalid",
            Self::Storage(_) => "Outbox storage is unavailable",
            Self::Busy => "Outbox storage is busy",
            Self::Database => "Outbox claim could not be stored",
        })
    }
}
impl Error for ReplyClaimError {}
impl From<sqlx::Error> for ReplyClaimError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

struct Candidate {
    id: Uuid,
    conversation_id: Uuid,
    operation_id: Uuid,
    inbound_id: Option<Uuid>,
    fence: Option<String>,
    memory_epoch: Uuid,
    owner_memory: bool,
}

#[derive(Deserialize)]
struct MemoryFence {
    target_epoch: String,
    source_epochs: BTreeMap<String, String>,
}

impl Database {
    /// Claim an ordinary neutral reply for a selected, authenticated destination.
    /// The claim carries no content. A caller revalidates sharing gates before sending.
    pub async fn claim_outbox_reply(
        &self,
        destination: ReplyDestination<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<ReplyClaim>, ReplyClaimError> {
        if !(1..=3600).contains(&lease_seconds)
            || !(1..=100).contains(&destination.channel.chars().count())
            || !(1..=200).contains(&destination.instance_id.chars().count())
        {
            return Err(ReplyClaimError::InvalidRequest);
        }
        let until = OffsetDateTime::from(now)
            .checked_add(time::Duration::seconds(i64::from(lease_seconds)))
            .and_then(|until| StoredInstant::try_from(until).ok())
            .ok_or(ReplyClaimError::InvalidRequest)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.work.begin().await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(ReplyClaimError::Storage)?;
            let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                .bind(REPLAY_MUTATION_LOCK)
                .fetch_one(&mut *txn)
                .await?;
            if !locked {
                return Err(ReplyClaimError::Busy);
            }
            let claim = next_claim(&mut txn, &destination, now, until).await?;
            txn.commit().await?;
            Ok(claim)
        })
        .await
        .map_err(|_| ReplyClaimError::Database)?
    }
}

async fn next_claim(
    txn: &mut Transaction<'_, Postgres>,
    destination: &ReplyDestination<'_>,
    now: StoredInstant,
    until: StoredInstant,
) -> Result<Option<ReplyClaim>, ReplyClaimError> {
    for _ in 0..CANDIDATES_PER_CLAIM {
        let Some(candidate) = candidate(txn, destination, now).await? else {
            return Ok(None);
        };
        if !valid_fence(txn, destination.owner_id, &candidate).await? {
            sqlx::query(
                "UPDATE outbox_messages SET state='cancelled',updated_at=now() WHERE id=$1",
            )
            .bind(candidate.id)
            .execute(&mut **txn)
            .await?;
            continue;
        }
        let token = Uuid::new_v4();
        sqlx::query(
            "UPDATE outbox_messages SET state='sending',lease_token=$2,lease_until=$3,
                     attempts=attempts+1,updated_at=now() WHERE id=$1",
        )
        .bind(candidate.id)
        .bind(token)
        .bind(OffsetDateTime::from(until))
        .execute(&mut **txn)
        .await?;
        return Ok(Some(ReplyClaim {
            outbox_id: candidate.id,
            lease_token: token,
            lease_until: until,
        }));
    }
    Ok(None)
}

async fn candidate(
    txn: &mut Transaction<'_, Postgres>,
    destination: &ReplyDestination<'_>,
    now: StoredInstant,
) -> Result<Option<Candidate>, sqlx::Error> {
    sqlx::query(
        "SELECT o.id,o.conversation_id,o.operation_id,o.inbound_message_id,o.memory_fence::text AS fence,
                c.memory_epoch,c.share_owner_memory
         FROM outbox_messages o JOIN conversations c ON c.id=o.conversation_id AND c.owner_id=o.owner_id
         WHERE o.owner_id=$1 AND c.channel=$2 AND c.channel_instance_id=$3
           AND o.state='queued' AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=$4)
           AND o.legacy_key IS NULL AND coalesce(o.intent->'initiative','false'::jsonb)='false'
           AND o.intent->>'owner_id'=o.owner_id::text AND o.intent->>'conversation_id'=o.conversation_id::text
           AND o.intent->>'intent_id'=o.id::text
           AND o.intent->'channel_instance'->>'channel'=$2 AND o.intent->'channel_instance'->>'instance_id'=$3
           AND NOT EXISTS(SELECT 1 FROM inbound_messages i WHERE i.id=o.inbound_message_id
                          AND i.legacy_telegram_update_id IS NOT NULL)
         ORDER BY o.created_at,o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED",
    )
    .bind(destination.owner_id)
    .bind(destination.channel)
    .bind(destination.instance_id)
    .bind(OffsetDateTime::from(now))
    .try_map(|row: PgRow| Ok(Candidate {
        id: row.try_get("id")?,
        conversation_id: row.try_get("conversation_id")?,
        operation_id: row.try_get("operation_id")?,
        inbound_id: row.try_get("inbound_message_id")?,
        fence: row.try_get("fence")?,
        memory_epoch: row.try_get("memory_epoch")?,
        owner_memory: row.try_get("share_owner_memory")?,
    }))
    .fetch_optional(&mut **txn)
    .await
}

async fn valid_fence(
    txn: &mut Transaction<'_, Postgres>,
    owner_id: Uuid,
    candidate: &Candidate,
) -> Result<bool, sqlx::Error> {
    let Some(fence) = candidate.fence.as_deref() else {
        return Ok(true);
    };
    let Ok(fence) = serde_json::from_str::<MemoryFence>(fence) else {
        return Ok(false);
    };
    if fence.target_epoch.parse().ok() != Some(candidate.memory_epoch)
        || (!candidate.owner_memory && !fence.source_epochs.is_empty())
    {
        return Ok(false);
    }
    if candidate.owner_memory {
        let sources: Option<BTreeMap<Uuid, Uuid>> = fence
            .source_epochs
            .into_iter()
            .map(|(id, epoch)| Some((id.parse().ok()?, epoch.parse().ok()?)))
            .collect();
        let Some(sources) = sources else {
            return Ok(false);
        };
        let sources: BTreeMap<_, _> = sources
            .into_iter()
            .map(|(id, epoch)| (id.to_string(), epoch.to_string()))
            .collect();
        let bound: bool = sqlx::query_scalar(
            "SELECT coalesce(jsonb_object_agg(id::text,memory_epoch::text),'{}')=$3::jsonb
             FROM conversations WHERE owner_id=$1 AND id<>$2 AND share_owner_memory",
        )
        .bind(owner_id)
        .bind(candidate.conversation_id)
        .bind(serde_json::json!(sources).to_string())
        .fetch_one(&mut **txn)
        .await?;
        if !bound {
            return Ok(false);
        }
    }
    sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM inbound_messages i
          WHERE i.id=$1 AND i.owner_id=$2 AND i.conversation_id=$3 AND i.operation_id=$4
            AND i.revision=(SELECT max(revision) FROM inbound_messages
                WHERE owner_id=$2 AND conversation_id=$3 AND operation_id=$4))",
    )
    .bind(candidate.inbound_id)
    .bind(owner_id)
    .bind(candidate.conversation_id)
    .bind(candidate.operation_id)
    .fetch_one(&mut **txn)
    .await
}
