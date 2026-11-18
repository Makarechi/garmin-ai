mod dispatch;
pub use dispatch::{ReplyDispatchError, ReplyDispatchResult, ReplyObservation, ReplySendOutcome};

use std::{collections::BTreeMap, error::Error, fmt, time::Duration};

use ga_domain::{
    channel::InboundEnvelope, telegram_control::TelegramControl, timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{Postgres, Row, Transaction, postgres::PgRow};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness, replay::REPLAY_MUTATION_LOCK,
    telegram_garmin::GarminProjection,
};

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

impl Candidate {
    fn read(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            id: row.try_get("id")?,
            conversation_id: row.try_get("conversation_id")?,
            operation_id: row.try_get("operation_id")?,
            inbound_id: row.try_get("inbound_message_id")?,
            fence: row.try_get("fence")?,
            memory_epoch: row.try_get("memory_epoch")?,
            owner_memory: row.try_get("share_owner_memory")?,
        })
    }
}

#[derive(Deserialize)]
struct MemoryFence {
    target_epoch: String,
    source_epochs: BTreeMap<String, String>,
    telegram_today: Option<GarminProjection>,
    telegram_status: Option<GarminProjection>,
    telegram_debug: Option<[Option<i64>; 2]>,
    telegram_goals: Option<u64>,
}

enum FenceStatus {
    Stale,
    Current(Option<GarminProjection>),
}

pub(crate) async fn control_reply_fence(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Uuid,
    state: Value,
) -> Result<String, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT (jsonb_build_object(
            'target_epoch',target.memory_epoch::text,
            'source_epochs',CASE WHEN target.share_owner_memory THEN coalesce((
                SELECT jsonb_object_agg(source.id::text,source.memory_epoch::text)
                FROM conversations source WHERE source.owner_id=target.owner_id
                AND source.id<>target.id AND source.share_owner_memory), '{}'::jsonb) ELSE '{}'::jsonb END
          ) || $2::jsonb)::text FROM conversations target WHERE id=$1",
    ).bind(conversation).bind(state.to_string()).fetch_one(&mut **txn).await
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
        if matches!(
            check_fence(txn, destination.owner_id, &candidate).await?,
            FenceStatus::Stale
        ) {
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
    .try_map(|row: PgRow| Candidate::read(&row))
    .fetch_optional(&mut **txn)
    .await
}

async fn check_fence(
    txn: &mut Transaction<'_, Postgres>,
    owner_id: Uuid,
    candidate: &Candidate,
) -> Result<FenceStatus, sqlx::Error> {
    // SQLAlchemy can persist an explicit None as JSON null.
    let Some(fence) = candidate.fence.as_deref().filter(|value| *value != "null") else {
        return Ok(FenceStatus::Current(None));
    };
    let Ok(fence) = serde_json::from_str::<MemoryFence>(fence) else {
        return Ok(FenceStatus::Stale);
    };
    let owner_control = match (
        fence.telegram_today.is_some(),
        fence.telegram_status.is_some(),
        fence.telegram_debug.is_some(),
        fence.telegram_goals.is_some(),
    ) {
        (true, false, false, false) => Some(TelegramControl::Today),
        (false, true, false, false) => Some(TelegramControl::Status),
        (false, false, true, false) => Some(TelegramControl::Debug),
        (false, false, false, true) => Some(TelegramControl::Goals),
        (false, false, false, false) => None,
        _ => return Ok(FenceStatus::Stale),
    };
    if owner_control.is_none()
        && (fence.target_epoch.parse().ok() != Some(candidate.memory_epoch)
            || (!candidate.owner_memory && !fence.source_epochs.is_empty()))
    {
        return Ok(FenceStatus::Stale);
    }
    if candidate.owner_memory && owner_control.is_none() {
        let sources: Option<BTreeMap<Uuid, Uuid>> = fence
            .source_epochs
            .into_iter()
            .map(|(id, epoch)| Some((id.parse().ok()?, epoch.parse().ok()?)))
            .collect();
        let Some(sources) = sources else {
            return Ok(FenceStatus::Stale);
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
            return Ok(FenceStatus::Stale);
        }
    }
    let inbound: Option<(bool, Option<String>)> = sqlx::query_as(
        "SELECT i.legacy_telegram_update_id IS NULL AND i.revision=(SELECT max(revision) FROM inbound_messages
                WHERE owner_id=$2 AND conversation_id=$3 AND operation_id=$4),
                CASE WHEN $5 THEN i.envelope::text END
          FROM inbound_messages i
          WHERE i.id=$1 AND i.owner_id=$2 AND i.conversation_id=$3 AND i.operation_id=$4
        ",
    )
    .bind(candidate.inbound_id)
    .bind(owner_id)
    .bind(candidate.conversation_id)
    .bind(candidate.operation_id)
    .bind(owner_control.is_some())
    .fetch_optional(&mut **txn)
    .await?;
    let Some((true, envelope)) = inbound else {
        return Ok(FenceStatus::Stale);
    };
    if owner_control.is_some()
        && envelope
            .as_deref()
            .and_then(|raw| InboundEnvelope::parse(raw.as_bytes()).ok())
            .as_ref()
            .and_then(TelegramControl::from_envelope)
            != owner_control
    {
        return Ok(FenceStatus::Stale);
    }
    if let Some(generation) = fence.telegram_debug {
        let unchanged: bool = sqlx::query_scalar(
            "SELECT coalesce((SELECT jsonb_build_array(value->'message_at',value->'update_id')
                 FROM app_state WHERE key='telegram:debug'), '[null,null]'::jsonb) = $1::jsonb",
        )
        .bind(serde_json::json!(generation).to_string())
        .fetch_one(&mut **txn)
        .await?;
        if !unchanged {
            return Ok(FenceStatus::Stale);
        }
    }
    if let Some(revision) = fence.telegram_goals {
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
            .bind(crate::personal_goals::GOAL_POLICY_LOCK)
            .fetch_one(&mut **txn)
            .await?;
        if !locked {
            return Err(sqlx::Error::Protocol(
                "Personal goals are being updated".into(),
            ));
        }
        let (goals, _) = crate::personal_goals::load(txn, owner_id)
            .await
            .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
        if revision != goals.revision {
            return Ok(FenceStatus::Stale);
        }
    }
    Ok(FenceStatus::Current(
        fence.telegram_today.or(fence.telegram_status),
    ))
}
