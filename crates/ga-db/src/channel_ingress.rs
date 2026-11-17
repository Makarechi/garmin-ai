mod callback;
mod native_telegram;
pub mod telegram_poll;
pub use callback::ChannelCallback;

use std::{error::Error, fmt, time::Duration};

use ga_domain::channel::{InboundEnvelope, InboundKind};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness, replay::REPLAY_MUTATION_LOCK};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InboundReceipt {
    pub message_id: Uuid,
    pub operation_id: Uuid,
    pub supersedes_id: Option<Uuid>,
    pub created: bool,
}

#[derive(Debug)]
pub enum IngressError {
    Storage(ReadinessError),
    Busy,
    ForeignConversation,
    MissingRevision,
    OldRevision,
    ActionUnavailable,
    InvalidEnvelope,
    Database,
}

impl fmt::Display for IngressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Inbound message storage is unavailable",
            Self::Busy => "Inbound message storage is busy",
            Self::ForeignConversation => "Conversation belongs to another channel binding",
            Self::MissingRevision => "Edited message has no earlier revision",
            Self::OldRevision => "Edited message revision is not newer",
            Self::ActionUnavailable => "Channel action is unavailable or already used",
            Self::InvalidEnvelope => "Resolved action does not match the authenticated envelope",
            Self::Database => "Inbound message could not be stored",
        })
    }
}
impl Error for IngressError {}
impl From<sqlx::Error> for IngressError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

type ExistingMessage = (Uuid, Uuid, Option<Uuid>, Uuid, Uuid);

impl Database {
    /// Persist a normalized envelope after its transport has verified the sender and owner.
    pub async fn ingest_channel_message(
        &self,
        envelope: &InboundEnvelope,
    ) -> Result<InboundReceipt, IngressError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_ingress().await?;
            let receipt = ingest(&mut txn, envelope).await?;
            txn.commit().await?;
            Ok(receipt)
        })
        .await
        .map_err(|_| IngressError::Database)?
    }
    async fn begin_ingress(&self) -> Result<Transaction<'static, Postgres>, IngressError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(IngressError::Storage)?;
        lock_mutations(&mut txn).await?;
        Ok(txn)
    }
}

async fn lock_mutations(txn: &mut Transaction<'_, Postgres>) -> Result<(), IngressError> {
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(IngressError::Busy);
    }
    Ok(())
}

async fn ingest(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
) -> Result<InboundReceipt, IngressError> {
    let channel = envelope.channel();
    let namespace = (channel.channel(), channel.instance_id());
    sqlx::query(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
         VALUES($1,$2,$3,$4,gen_random_uuid(),'{}',false) ON CONFLICT(id) DO NOTHING",
    )
    .bind(envelope.conversation_id()).bind(envelope.owner_id()).bind(namespace.0).bind(namespace.1)
    .execute(&mut **txn).await?;
    let binding: (Uuid, String, String) = sqlx::query_as(
        "SELECT owner_id,channel,channel_instance_id FROM conversations WHERE id=$1",
    )
    .bind(envelope.conversation_id())
    .fetch_one(&mut **txn)
    .await?;
    if (binding.0, binding.1.as_str(), binding.2.as_str())
        != (envelope.owner_id(), namespace.0, namespace.1)
    {
        return Err(IngressError::ForeignConversation);
    }
    if let Some(stored) = existing(txn, envelope).await? {
        return check_duplicate(stored, envelope);
    }
    let (supersedes_id, operation_id) = if envelope.kind() == InboundKind::Edit {
        let (id, operation, revision, owner, conversation): (Uuid, Uuid, i32, Uuid, Uuid) = sqlx::query_as(
            "SELECT id,operation_id,revision,owner_id,conversation_id
             FROM inbound_messages WHERE channel=$1 AND channel_instance_id=$2 AND external_message_id=$3
             ORDER BY revision DESC LIMIT 1",
        ).bind(namespace.0).bind(namespace.1).bind(envelope.external_message_id())
            .fetch_optional(&mut **txn).await?.ok_or(IngressError::MissingRevision)?;
        if (owner, conversation) != (envelope.owner_id(), envelope.conversation_id()) {
            return Err(IngressError::ForeignConversation);
        }
        if revision >= envelope.revision() {
            return Err(IngressError::OldRevision);
        }
        (Some(id), operation)
    } else {
        let operation = (envelope.kind() == InboundKind::Action)
            .then(|| envelope.action_operation())
            .flatten()
            .unwrap_or_else(Uuid::new_v4);
        (None, operation)
    };
    let new = sqlx::query_scalar::<_, Uuid>(
        "INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,external_event_id,
             external_message_id,sender_ref,occurred_at,received_at,kind,normalized_text,envelope,revision,status,operation_id,supersedes_id)
         VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,'pending',$15,$16)
         ON CONFLICT ON CONSTRAINT uq_inbound_transport_revision DO NOTHING RETURNING id",
    )
    .bind(envelope.message_id()).bind(envelope.owner_id()).bind(envelope.conversation_id())
    .bind(namespace.0).bind(namespace.1).bind(envelope.external_event_id()).bind(envelope.external_message_id())
    .bind(envelope.sender_ref()).bind(envelope.occurred_at()).bind(envelope.received_at())
    .bind(envelope.kind().label()).bind(envelope.text()).bind(envelope.document().to_string())
    .bind(envelope.revision()).bind(operation_id).bind(supersedes_id)
    .fetch_optional(&mut **txn).await?;
    if let Some(message_id) = new {
        return Ok(InboundReceipt {
            message_id,
            operation_id,
            supersedes_id,
            created: true,
        });
    }
    check_duplicate(
        existing(txn, envelope)
            .await?
            .ok_or(IngressError::Database)?,
        envelope,
    )
}

async fn existing(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
) -> Result<Option<ExistingMessage>, IngressError> {
    Ok(sqlx::query_as(
        "SELECT id,operation_id,supersedes_id,owner_id,conversation_id FROM inbound_messages
         WHERE channel=$1 AND channel_instance_id=$2 AND external_event_id=$3 AND revision=$4",
    )
    .bind(envelope.channel().channel())
    .bind(envelope.channel().instance_id())
    .bind(envelope.external_event_id())
    .bind(envelope.revision())
    .fetch_optional(&mut **txn)
    .await?)
}

fn check_duplicate(
    stored: ExistingMessage,
    envelope: &InboundEnvelope,
) -> Result<InboundReceipt, IngressError> {
    let (message_id, operation_id, supersedes_id, owner_id, conversation_id) = stored;
    if (owner_id, conversation_id) != (envelope.owner_id(), envelope.conversation_id()) {
        return Err(IngressError::ForeignConversation);
    }
    Ok(InboundReceipt {
        message_id,
        operation_id,
        supersedes_id,
        created: false,
    })
}
