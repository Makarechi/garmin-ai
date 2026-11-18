use std::{error::Error, fmt, future::Future, time::Duration};

use ga_domain::{channel::telegram_conversation_id, timestamp::StoredInstant};
use serde_json::Value;
use sqlx::{Connection, PgConnection, Postgres, Row, Transaction, postgres::PgRow};
use time::OffsetDateTime;
use tokio::time::Instant;
use uuid::Uuid;

use super::{Candidate, FenceStatus, ReplyClaim, ReplyDestination, check_fence};
use crate::{
    Database, OWNER_ENROLLMENT_LOCK, ReadinessError, STORAGE_LIFECYCLE_LOCK,
    channel_ingress::telegram_poll::{CHANNEL_SELECTION_LOCK, TelegramPollError, selection},
    check_readiness,
    delivery::{DeliveryReceipt, DeliveryState, DeliveryTarget, record_delivery_receipt},
    jobs::{STORAGE_QUEUE_LOCK, storage_notices::StorageNoticeFence},
    replay::REPLAY_MUTATION_LOCK,
    share_policy::{CHANNEL_CONSENT_LOCK, ShareRequirement, intent_sharing_allowed},
    telegram_binding::{CHANNEL_BINDING_LOCK, TelegramBindingError, confirmed_pairing},
    telegram_garmin::REPLAY_NOTICE,
};

const DATABASE_DEADLINE: Duration = Duration::from_millis(1500);

pub struct ReplyObservation {
    pub state: DeliveryState,
    pub observed_at: StoredInstant,
    pub provider_reference: Option<String>,
    pub detail: Option<String>,
}

pub enum ReplySendOutcome {
    Observation(ReplyObservation),
    /// The provider explicitly declined the whole request before any acceptance.
    RetryAt(StoredInstant),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReplyDispatchResult {
    ClaimUnavailable,
    Cancelled,
    Deferred {
        retry_at: StoredInstant,
    },
    Recorded {
        evidence_id: Uuid,
        state: DeliveryState,
    },
    Uncertain,
}

#[derive(Debug)]
pub enum ReplyDispatchError {
    Storage(ReadinessError),
    Busy,
    Database,
}

impl fmt::Display for ReplyDispatchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Outbox delivery storage is unavailable",
            Self::Busy => "Outbox delivery permissions are changing",
            Self::Database => "Outbox delivery could not be prepared",
        })
    }
}
impl Error for ReplyDispatchError {}
impl From<sqlx::Error> for ReplyDispatchError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

enum Dispatch {
    Unavailable,
    Cancelled,
    Started {
        intent: Value,
        token: Uuid,
        deadline: Instant,
        clock: (StoredInstant, Instant),
        storage_notice: Option<StorageNoticeFence>,
    },
}

impl Database {
    /// Reschedule a claim whose preparation failed before starting any provider request.
    /// Once dispatch rotates its token, the original claim can never requeue the send.
    pub async fn defer_unsent_reply(
        &self,
        owner: Uuid,
        claim: ReplyClaim,
        retry_at: StoredInstant,
    ) -> Result<bool, ReplyDispatchError> {
        tokio::time::timeout(DATABASE_DEADLINE, async {
            let mut txn = self.work.begin().await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(ReplyDispatchError::Storage)?;
            let updated = defer(
                &mut txn,
                owner,
                claim.outbox_id,
                claim.lease_token,
                retry_at,
            )
            .await?;
            txn.commit().await?;
            Ok(updated)
        })
        .await
        .map_err(|_| ReplyDispatchError::Database)?
    }

    /// Expose one reply to its channel after fresh authorization and durable dispatch fencing.
    /// The closure returns the provider observation or a definitely safe retry time.
    pub async fn dispatch_outbox_reply<F>(
        &self,
        destination: ReplyDestination<'_>,
        claim: ReplyClaim,
        requirements: &[ShareRequirement<'_>],
        send: impl FnOnce(Value) -> F,
    ) -> Result<ReplyDispatchResult, ReplyDispatchError>
    where
        F: Future<Output = Option<ReplySendOutcome>>,
    {
        if destination.channel == "telegram" {
            return Ok(ReplyDispatchResult::ClaimUnavailable);
        }
        self.dispatch_reply(destination, claim, requirements, None, send)
            .await
    }

    /// Deliver to the configured Telegram chat under its current pairing and
    /// onboarding screen for the full duration of provider network I/O.
    pub async fn dispatch_telegram_outbox_reply<F>(
        &self,
        destination: ReplyDestination<'_>,
        expected_chat: i64,
        claim: ReplyClaim,
        requirements: &[ShareRequirement<'_>],
        send: impl FnOnce(Value) -> F,
    ) -> Result<ReplyDispatchResult, ReplyDispatchError>
    where
        F: Future<Output = Option<ReplySendOutcome>>,
    {
        if destination.channel != "telegram" {
            return Ok(ReplyDispatchResult::ClaimUnavailable);
        }
        self.dispatch_reply(destination, claim, requirements, Some(expected_chat), send)
            .await
    }

    async fn dispatch_reply<F>(
        &self,
        destination: ReplyDestination<'_>,
        claim: ReplyClaim,
        requirements: &[ShareRequirement<'_>],
        expected_chat: Option<i64>,
        send: impl FnOnce(Value) -> F,
    ) -> Result<ReplyDispatchResult, ReplyDispatchError>
    where
        F: Future<Output = Option<ReplySendOutcome>>,
    {
        let (mut connection, dispatch) = tokio::time::timeout(DATABASE_DEADLINE, async {
            let options = self.work.connect_options();
            let mut connection = PgConnection::connect_with(options.as_ref()).await?;
            let prepared = prepare(
                &mut connection,
                &destination,
                claim,
                requirements,
                expected_chat,
            )
            .await?;
            Ok::<_, ReplyDispatchError>((connection, prepared))
        })
        .await
        .map_err(|_| ReplyDispatchError::Database)??;
        let (intent, token, deadline, (wall, started), storage_notice) = match dispatch {
            Dispatch::Unavailable => return Ok(ReplyDispatchResult::ClaimUnavailable),
            Dispatch::Cancelled => return Ok(ReplyDispatchResult::Cancelled),
            Dispatch::Started {
                intent,
                token,
                deadline,
                clock,
                storage_notice,
            } => (intent, token, deadline, clock, storage_notice),
        };
        let notice = storage_notice
            .as_ref()
            .map(|notice| (notice, destination.instance_id));
        let outcome = tokio::time::timeout_at(deadline, async {
            if Instant::now() >= deadline {
                return None;
            }
            send(intent).await
        })
        .await
        .ok()
        .flatten();
        let inferred_clock = || {
            let elapsed = i64::try_from(started.elapsed().as_micros()).unwrap_or(i64::MAX);
            StoredInstant::from_unix_microseconds(wall.unix_microseconds().saturating_add(elapsed))
                .unwrap_or(wall)
        };
        if let Some(ReplySendOutcome::RetryAt(retry_at)) = outcome {
            let retry_at = retry_at.max(inferred_clock());
            let deferred = tokio::time::timeout(
                DATABASE_DEADLINE,
                defer_after_send(
                    &mut connection,
                    destination.owner_id,
                    claim.outbox_id,
                    token,
                    retry_at,
                    notice,
                ),
            )
            .await;
            return Ok(if matches!(deferred, Ok(Ok(true))) {
                ReplyDispatchResult::Deferred { retry_at }
            } else {
                ReplyDispatchResult::Uncertain
            });
        }
        let observation = match outcome {
            Some(ReplySendOutcome::Observation(observation))
                if !matches!(
                    observation.state,
                    DeliveryState::Queued | DeliveryState::Sending
                ) =>
            {
                observation
            }
            _ => ReplyObservation {
                state: DeliveryState::Uncertain,
                observed_at: inferred_clock(),
                provider_reference: None,
                detail: None,
            },
        };
        let recorded = tokio::time::timeout(
            DATABASE_DEADLINE,
            persist(
                &mut connection,
                destination.owner_id,
                claim.outbox_id,
                token,
                &observation,
                notice,
            ),
        )
        .await;
        Ok(match recorded {
            Ok(Ok(evidence_id)) => ReplyDispatchResult::Recorded {
                evidence_id,
                state: observation.state,
            },
            // The original token was already retired before any send. A failed
            // write leaves the rotated lease for recovery as an uncertain send.
            _ => ReplyDispatchResult::Uncertain,
        })
    }
}

async fn prepare(
    connection: &mut PgConnection,
    destination: &ReplyDestination<'_>,
    claim: ReplyClaim,
    requirements: &[ShareRequirement<'_>],
    expected_chat: Option<i64>,
) -> Result<Dispatch, ReplyDispatchError> {
    for lock in [
        STORAGE_LIFECYCLE_LOCK,
        OWNER_ENROLLMENT_LOCK,
        REPLAY_MUTATION_LOCK,
        CHANNEL_CONSENT_LOCK,
    ] {
        acquire_shared(connection, lock).await?;
    }
    if expected_chat.is_some() {
        for lock in [CHANNEL_SELECTION_LOCK, CHANNEL_BINDING_LOCK] {
            acquire_shared(connection, lock).await?;
        }
    }
    let mut txn = connection.begin().await?;
    sqlx::query("SET LOCAL statement_timeout='750ms'")
        .execute(&mut *txn)
        .await?;
    check_readiness(&mut txn)
        .await
        .map_err(ReplyDispatchError::Storage)?;
    let row = sqlx::query(
        "SELECT o.id,o.conversation_id,o.operation_id,o.inbound_message_id,o.memory_fence::text AS fence,
                c.memory_epoch,c.share_owner_memory,o.intent::text AS intent
         FROM outbox_messages o JOIN conversations c ON c.id=o.conversation_id AND c.owner_id=o.owner_id
         WHERE o.id=$1 AND o.state='sending' AND o.lease_token=$2 AND o.lease_until>clock_timestamp()
           AND o.owner_id=$3 AND c.channel=$4 AND c.channel_instance_id=$5
           AND o.legacy_key IS NULL
           AND NOT EXISTS(SELECT 1 FROM inbound_messages i WHERE i.id=o.inbound_message_id
                      AND i.legacy_telegram_update_id IS NOT NULL)
         FOR UPDATE OF o NOWAIT",
    )
    .bind(claim.outbox_id)
    .bind(claim.lease_token)
    .bind(destination.owner_id)
    .bind(destination.channel)
    .bind(destination.instance_id)
    .try_map(|row: PgRow| Ok((Candidate::read(&row)?, row.try_get::<String, _>("intent")?)))
    .fetch_optional(&mut *txn)
    .await?;
    let Some((candidate, raw)) = row else {
        txn.rollback().await?;
        return Ok(Dispatch::Unavailable);
    };
    if let Some(chat) = expected_chat
        && (candidate.conversation_id
            != telegram_conversation_id(destination.owner_id, destination.instance_id, chat)
            || !telegram_authorized(&mut txn, destination, chat).await?)
    {
        close_claim(&mut txn, claim.outbox_id, "cancelled").await?;
        txn.commit().await?;
        return Ok(Dispatch::Cancelled);
    }
    let share_target = format!("{}:{}", destination.channel, destination.instance_id);
    let storage_notice = candidate.storage_notice();
    if storage_notice.is_some() {
        acquire_shared(&mut txn, STORAGE_QUEUE_LOCK).await?;
    }
    let fence = check_fence(&mut txn, destination, &candidate).await?;
    let ready = match (serde_json::from_str::<Value>(&raw), fence) {
        (Ok(mut intent), FenceStatus::Current(projection))
            if matches_intent(&intent, destination, &candidate)
                && intent_sharing_allowed(&mut txn, &share_target, &intent, requirements)
                    .await
                    .map_err(|_| ReplyDispatchError::Database)? =>
        {
            if let Some(projection) = projection
                && projection.requires_notice(&mut txn).await?
            {
                intent["blocks"] = serde_json::json!([{"text":REPLAY_NOTICE}]);
                sqlx::query("UPDATE outbox_messages SET intent=$2::jsonb WHERE id=$1")
                    .bind(candidate.id)
                    .bind(intent.to_string())
                    .execute(&mut *txn)
                    .await?;
            }
            Some(intent)
        }
        _ => None,
    };
    let Some(intent) = ready else {
        close_claim(&mut txn, claim.outbox_id, "cancelled").await?;
        txn.commit().await?;
        return Ok(Dispatch::Cancelled);
    };
    let started = Instant::now();
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut *txn)
        .await?;
    let expiry = if let Some(value) = intent.get("expires_at").filter(|value| !value.is_null()) {
        let Some(expiry) = StoredInstant::from_legacy_input(value).map(OffsetDateTime::from) else {
            close_claim(&mut txn, claim.outbox_id, "cancelled").await?;
            txn.commit().await?;
            return Ok(Dispatch::Cancelled);
        };
        if expiry <= now {
            close_claim(&mut txn, claim.outbox_id, "expired").await?;
            txn.commit().await?;
            return Ok(Dispatch::Cancelled);
        }
        Some(expiry)
    } else {
        None
    };
    let token = Uuid::new_v4();
    let locked: Option<OffsetDateTime> = sqlx::query_scalar(
        "UPDATE outbox_messages SET lease_token=$2,updated_at=now() WHERE id=$1 AND lease_until>clock_timestamp()
         RETURNING lease_until",
    )
    .bind(claim.outbox_id).bind(token).fetch_optional(&mut *txn).await?;
    let Some(until) = locked else {
        txn.rollback().await?;
        return Ok(Dispatch::Unavailable);
    };
    let remaining = Duration::try_from(expiry.map_or(until, |expiry| until.min(expiry)) - now)
        .map_err(|_| ReplyDispatchError::Database)?;
    let wall = StoredInstant::try_from(now).map_err(|_| ReplyDispatchError::Database)?;
    if let Some(notice) = &storage_notice {
        notice
            .start(&mut txn, destination.instance_id, wall)
            .await?;
    }
    txn.commit().await?;
    Ok(Dispatch::Started {
        intent,
        token,
        deadline: started + remaining,
        clock: (wall, started),
        storage_notice,
    })
}

async fn acquire_shared(
    connection: &mut PgConnection,
    lock: i64,
) -> Result<(), ReplyDispatchError> {
    let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock_shared($1)")
        .bind(lock)
        .fetch_one(connection)
        .await?;
    if acquired {
        Ok(())
    } else {
        Err(ReplyDispatchError::Busy)
    }
}

async fn telegram_authorized(
    txn: &mut Transaction<'_, Postgres>,
    destination: &ReplyDestination<'_>,
    expected_chat: i64,
) -> Result<bool, ReplyDispatchError> {
    let selected = selection(txn, destination.instance_id)
        .await
        .map_err(|error| {
            if matches!(error, TelegramPollError::Busy) {
                ReplyDispatchError::Busy
            } else {
                ReplyDispatchError::Database
            }
        })?;
    if !selected {
        return Ok(false);
    }
    match confirmed_pairing(txn, destination.instance_id, expected_chat).await {
        Ok(owner) => Ok(owner == destination.owner_id),
        Err(TelegramBindingError::Missing | TelegramBindingError::Mismatch) => Ok(false),
        Err(TelegramBindingError::Busy) => Err(ReplyDispatchError::Busy),
        Err(_) => Err(ReplyDispatchError::Database),
    }
}

fn matches_intent(intent: &Value, destination: &ReplyDestination<'_>, row: &Candidate) -> bool {
    intent["intent_id"]
        .as_str()
        .and_then(|value| value.parse().ok())
        == Some(row.id)
        && intent["owner_id"]
            .as_str()
            .and_then(|value| value.parse().ok())
            == Some(destination.owner_id)
        && intent["conversation_id"]
            .as_str()
            .and_then(|value| value.parse().ok())
            == Some(row.conversation_id)
        && intent["channel_instance"]["channel"].as_str() == Some(destination.channel)
        && intent["channel_instance"]["instance_id"].as_str() == Some(destination.instance_id)
        && matches!(intent.get("initiative"), None | Some(Value::Bool(false)))
}

async fn close_claim(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    state: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE outbox_messages SET state=$2,lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=$1")
        .bind(id).bind(state).execute(&mut **txn).await?;
    Ok(())
}

async fn defer(
    connection: &mut PgConnection,
    owner: Uuid,
    outbox: Uuid,
    token: Uuid,
    retry_at: StoredInstant,
) -> Result<bool, sqlx::Error> {
    let updated = sqlx::query(
        "UPDATE outbox_messages SET state='queued',next_attempt_at=$4,
                   lease_token=NULL,lease_until=NULL,updated_at=now()
         WHERE id=$1 AND owner_id=$2 AND lease_token=$3 AND state='sending'",
    )
    .bind(outbox)
    .bind(owner)
    .bind(token)
    .bind(OffsetDateTime::from(retry_at))
    .execute(connection)
    .await?;
    Ok(updated.rows_affected() == 1)
}

async fn persist(
    connection: &mut PgConnection,
    owner_id: Uuid,
    outbox_id: Uuid,
    lease_token: Uuid,
    observation: &ReplyObservation,
    notice: Option<(&StorageNoticeFence, &str)>,
) -> Result<Uuid, ReplyDispatchError> {
    let mut txn = connection.begin().await?;
    let result = record_delivery_receipt(
        &mut txn,
        DeliveryTarget {
            owner_id,
            outbox_id,
            lease_token: Some(lease_token),
        },
        DeliveryReceipt {
            id: Uuid::new_v4(),
            intent_id: outbox_id,
            state: observation.state,
            observed_at: observation.observed_at,
            provider_reference: observation.provider_reference.as_deref(),
            detail: observation.detail.as_deref(),
        },
    )
    .await;
    match result {
        Ok(id) => {
            if let Some((notice, instance)) = notice {
                notice
                    .complete(
                        &mut txn,
                        instance,
                        observation.state,
                        observation.provider_reference.as_deref(),
                    )
                    .await?;
            }
            txn.commit().await?;
            Ok(id)
        }
        Err(_) => {
            txn.rollback().await?;
            Err(ReplyDispatchError::Database)
        }
    }
}

async fn defer_after_send(
    connection: &mut PgConnection,
    owner: Uuid,
    outbox: Uuid,
    token: Uuid,
    retry_at: StoredInstant,
    notice: Option<(&StorageNoticeFence, &str)>,
) -> Result<bool, sqlx::Error> {
    let Some((notice, instance)) = notice else {
        return defer(connection, owner, outbox, token, retry_at).await;
    };
    let mut txn = connection.begin().await?;
    let deferred = defer(&mut txn, owner, outbox, token, retry_at).await?;
    if deferred {
        notice.retry(&mut txn, instance, retry_at).await?;
    }
    txn.commit().await?;
    Ok(deferred)
}
