//! Scoped analytic context for neutral conversations.

mod confirmed;
pub use confirmed::ConfirmedAnalysis;

use std::{collections::BTreeMap, error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness, replay::REPLAY_MUTATION_LOCK};

const RECENT_MICROS: i64 = 7 * 24 * 60 * 60 * 1_000_000;
const TURN_LIMIT: usize = 6;
const BYTE_LIMIT: usize = 12_000;
const DEADLINE: Duration = Duration::from_millis(1500);

#[derive(Clone, Copy)]
pub struct ConversationBinding {
    pub owner_id: Uuid,
    pub conversation_id: Uuid,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MemoryFence {
    pub target_epoch: Uuid,
    pub source_epochs: BTreeMap<Uuid, Uuid>,
}

pub struct MemorySnapshot {
    pub fence: MemoryFence,
    pub turns: Vec<Value>,
}

#[derive(Debug)]
pub enum MemoryError {
    NotFound,
    Unconfirmed,
    Storage(ReadinessError),
    Busy,
    InvalidState,
    Database,
}

impl fmt::Display for MemoryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NotFound => "Conversation is unavailable to this owner",
            Self::Unconfirmed => "Analysis requires its confirmed question and answer",
            Self::Storage(_) => "Conversation storage is unavailable",
            Self::Busy => "Conversation context is being updated",
            Self::InvalidState => "Stored conversation context is invalid",
            Self::Database => "Conversation context could not be accessed",
        })
    }
}
impl Error for MemoryError {}
impl From<sqlx::Error> for MemoryError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

struct Conversation {
    id: Uuid,
    epoch: Uuid,
    shared: bool,
    turns: Vec<Value>,
}

impl Database {
    /// Read bounded context and the exact epochs that authorized it in one transaction.
    pub async fn conversation_memory(
        &self,
        binding: ConversationBinding,
        now: StoredInstant,
    ) -> Result<MemorySnapshot, MemoryError> {
        tokio::time::timeout(DEADLINE, async {
            let mut txn = self.begin_memory().await?;
            let snapshot = snapshot(&mut txn, binding, now).await?;
            txn.commit().await?;
            Ok(snapshot)
        })
        .await
        .map_err(|_| MemoryError::Database)?
    }

    /// Clear one conversation's state and invalidate answers generated from it.
    pub async fn forget_conversation_memory(
        &self,
        binding: ConversationBinding,
    ) -> Result<Uuid, MemoryError> {
        tokio::time::timeout(DEADLINE, async {
            let mut txn = self.begin_memory().await?;
            let epoch = sqlx::query_scalar(
                "UPDATE conversations SET state='{}',memory_epoch=gen_random_uuid(),updated_at=now()
                 WHERE id=$1 AND owner_id=$2 RETURNING memory_epoch",
            ).bind(binding.conversation_id).bind(binding.owner_id)
              .fetch_optional(&mut *txn).await?.ok_or(MemoryError::NotFound)?;
            txn.commit().await?;
            Ok(epoch)
        })
        .await
        .map_err(|_| MemoryError::Database)?
    }

    /// Change one conversation's opt-in, and retire affected turns when sharing is withdrawn.
    pub async fn set_conversation_memory_sharing(
        &self,
        binding: ConversationBinding,
        enabled: bool,
    ) -> Result<Uuid, MemoryError> {
        tokio::time::timeout(DEADLINE, async {
            let mut txn = self.begin_memory().await?;
            let row: Option<(Uuid, bool)> = sqlx::query_as(
                "SELECT memory_epoch,share_owner_memory FROM conversations WHERE id=$1 AND owner_id=$2",
            ).bind(binding.conversation_id).bind(binding.owner_id).fetch_optional(&mut *txn).await?;
            let Some((epoch, previous)) = row else {
                return Err(MemoryError::NotFound);
            };
            if previous == enabled {
                txn.rollback().await?;
                return Ok(epoch);
            }
            let epoch = sqlx::query_scalar(
                "UPDATE conversations SET share_owner_memory=$3,memory_epoch=gen_random_uuid(),updated_at=now()
                 WHERE id=$1 AND owner_id=$2 RETURNING memory_epoch",
            ).bind(binding.conversation_id).bind(binding.owner_id).bind(enabled)
              .fetch_one(&mut *txn).await?;
            if !enabled {
                retire_dependencies(&mut txn, binding).await?;
            }
            txn.commit().await?;
            Ok(epoch)
        }).await.map_err(|_| MemoryError::Database)?
    }

    async fn begin_memory(&self) -> Result<Transaction<'static, Postgres>, MemoryError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout='750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(MemoryError::Storage)?;
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(REPLAY_MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !locked {
            return Err(MemoryError::Busy);
        }
        Ok(txn)
    }
}

type ConversationRow = (Uuid, Uuid, bool, Option<String>);

async fn snapshot(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    now: StoredInstant,
) -> Result<MemorySnapshot, MemoryError> {
    let target = conversation(txn, binding).await?;
    let mut sources = vec![target];
    if sources[0].shared {
        let shared: Vec<ConversationRow> = sqlx::query_as(
            "SELECT id,memory_epoch,share_owner_memory,(state->'analysis_turns')::text
             FROM conversations WHERE owner_id=$1 AND id<>$2 AND share_owner_memory",
        )
        .bind(binding.owner_id)
        .bind(binding.conversation_id)
        .fetch_all(&mut **txn)
        .await?;
        for row in shared {
            sources.push(parse_row(row)?);
        }
    }
    let target_epoch = sources[0].epoch;
    let shared_epochs: BTreeMap<_, _> = sources
        .iter()
        .filter(|source| source.shared)
        .map(|source| (source.id, source.epoch))
        .collect();
    let mut turns = Vec::new();
    for source in sources {
        let recent = recent_turns(&source.turns, source.shared, &shared_epochs, now)?;
        if recent.len() != source.turns.len() {
            persist_turns(
                txn,
                ConversationBinding {
                    conversation_id: source.id,
                    ..binding
                },
                &recent,
            )
            .await?;
        }
        for turn in recent {
            let mut turn = turn.clone();
            turn["conversation_id"] = json!(source.id.to_string());
            turns.push(turn);
        }
    }
    let turns = bounded_turns(turns)?;
    let mut source_epochs = shared_epochs;
    source_epochs.remove(&binding.conversation_id);
    Ok(MemorySnapshot {
        fence: MemoryFence {
            target_epoch,
            source_epochs,
        },
        turns,
    })
}

async fn conversation(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
) -> Result<Conversation, MemoryError> {
    let target: ConversationRow = sqlx::query_as(
        "SELECT id,memory_epoch,share_owner_memory,(state->'analysis_turns')::text
         FROM conversations WHERE id=$1 AND owner_id=$2",
    )
    .bind(binding.conversation_id)
    .bind(binding.owner_id)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(MemoryError::NotFound)?;
    parse_row(target)
}

async fn persist_turns(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    turns: impl serde::Serialize,
) -> Result<(), MemoryError> {
    let turns = serde_json::to_string(&turns).map_err(|_| MemoryError::InvalidState)?;
    sqlx::query(
        "UPDATE conversations SET state=jsonb_set(state,'{analysis_turns}',$3::jsonb),updated_at=now()
         WHERE id=$1 AND owner_id=$2",
    ).bind(binding.conversation_id).bind(binding.owner_id).bind(turns).execute(&mut **txn).await?;
    Ok(())
}

fn bounded_turns(turns: Vec<Value>) -> Result<Vec<Value>, MemoryError> {
    let mut dated = turns
        .into_iter()
        .map(|turn| Ok((asked_at(&turn)?, turn)))
        .collect::<Result<Vec<_>, MemoryError>>()?;
    dated.sort_by(|(left_time, left), (right_time, right)| {
        left_time.cmp(right_time).then_with(|| {
            left["operation_id"]
                .as_str()
                .cmp(&right["operation_id"].as_str())
        })
    });
    let first = dated.len().saturating_sub(TURN_LIMIT);
    let mut turns: Vec<_> = dated
        .into_iter()
        .skip(first)
        .map(|(_, turn)| turn)
        .collect();
    while python_json_bytes(&turns)? > BYTE_LIMIT {
        turns.remove(0);
    }
    Ok(turns)
}

fn parse_row((id, epoch, shared, state): ConversationRow) -> Result<Conversation, MemoryError> {
    let turns = state
        .as_deref()
        .map(serde_json::from_str::<Vec<Value>>)
        .transpose()
        .map_err(|_| MemoryError::InvalidState)?
        .unwrap_or_default();
    Ok(Conversation {
        id,
        epoch,
        shared,
        turns,
    })
}

fn recent_turns<'a>(
    turns: &'a [Value],
    shared: bool,
    shared_epochs: &BTreeMap<Uuid, Uuid>,
    now: StoredInstant,
) -> Result<Vec<&'a Value>, MemoryError> {
    let mut recent = Vec::new();
    for turn in &turns[turns.len().saturating_sub(TURN_LIMIT)..] {
        let asked = asked_at(turn)?;
        let elapsed = now
            .unix_microseconds()
            .saturating_sub(asked.unix_microseconds());
        if !(0..=RECENT_MICROS).contains(&elapsed) {
            continue;
        }
        if let Some(dependencies) = turn.get("source_epochs") {
            let dependencies = dependencies.as_object().ok_or(MemoryError::InvalidState)?;
            let active = dependencies.iter().all(|(source, epoch)| {
                let current = source
                    .parse::<Uuid>()
                    .ok()
                    .and_then(|source| shared_epochs.get(&source));
                let pinned = epoch.as_str().and_then(|epoch| epoch.parse::<Uuid>().ok());
                matches!((current, pinned), (Some(current), Some(pinned)) if *current == pinned)
            });
            if !dependencies.is_empty() && (!shared || !active) {
                continue;
            }
        }
        recent.push(turn);
    }
    Ok(recent)
}

fn asked_at(turn: &Value) -> Result<StoredInstant, MemoryError> {
    turn["asked_at"]
        .as_str()
        .and_then(|time| time.parse().ok())
        .ok_or(MemoryError::InvalidState)
}

fn python_json_bytes(turns: &[Value]) -> Result<usize, MemoryError> {
    let mut bytes = Vec::new();
    let mut writer = serde_json::Serializer::with_formatter(&mut bytes, PythonSpacing);
    serde::Serialize::serialize(turns, &mut writer).map_err(|_| MemoryError::InvalidState)?;
    Ok(bytes.len())
}

struct PythonSpacing;
impl serde_json::ser::Formatter for PythonSpacing {
    fn begin_array_value<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }
    fn begin_object_key<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }
    fn begin_object_value<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
    ) -> std::io::Result<()> {
        writer.write_all(b": ")
    }
}

async fn retire_dependencies(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
) -> Result<(), MemoryError> {
    sqlx::query(
        "WITH retained AS (
            SELECT conversation.id, coalesce(jsonb_agg(turn ORDER BY position) FILTER (
                WHERE NOT (
                    (conversation.id=$1 AND coalesce(turn->'source_epochs','{}'::jsonb) <> '{}'::jsonb)
                    OR coalesce(turn->'source_epochs' ? $3,false)
                )
            ),'[]'::jsonb) AS turns
            FROM conversations conversation
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(conversation.state->'analysis_turns')='array'
                     THEN conversation.state->'analysis_turns' ELSE '[]'::jsonb END
            )
              WITH ORDINALITY AS stored(turn,position)
            WHERE conversation.owner_id=$2
            GROUP BY conversation.id
         )
         UPDATE conversations SET state=jsonb_set(conversations.state,'{analysis_turns}',retained.turns),updated_at=now()
         FROM retained WHERE conversations.id=retained.id
           AND retained.turns IS DISTINCT FROM conversations.state->'analysis_turns'",
    ).bind(binding.conversation_id).bind(binding.owner_id).bind(binding.conversation_id.to_string())
     .execute(&mut **txn).await?;
    Ok(())
}
