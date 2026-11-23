//! Explicit owner goals and their revision, shared by channels and analysis.

use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness, replay::REPLAY_MUTATION_LOCK};

const KEY: &str = "preferences:personal-goals";
pub(crate) const GOAL_POLICY_LOCK: i64 = 72_104_626;

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PersonalGoal {
    Migraine,
    Running,
    Sleep,
    Wellbeing,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct PersonalGoals {
    pub configured: bool,
    pub revision: u64,
    pub goals: Vec<PersonalGoal>,
    pub updated_at: Option<String>,
}

#[derive(Clone, Copy)]
pub struct GoalCommandOrder<'a> {
    pub source_epoch: i64,
    pub received_at: StoredInstant,
    pub tie_breaker: &'a str,
}

pub struct GoalSelection<'a> {
    pub expected_revision: u64,
    pub goals: &'a [PersonalGoal],
    pub now: StoredInstant,
    pub command: Option<GoalCommandOrder<'a>>,
}

#[derive(Debug)]
pub enum PersonalGoalsError {
    Storage(ReadinessError),
    Owner,
    Busy,
    InvalidSelection,
    InvalidState,
    Conflict,
    Database,
}

impl fmt::Display for PersonalGoalsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Owner preferences storage is unavailable",
            Self::Owner => "Personal goals belong to a different owner",
            Self::Busy => "Personal goals are being updated",
            Self::InvalidSelection => "Personal goals must be distinct valid choices",
            Self::InvalidState => "Saved personal goals are invalid",
            Self::Conflict => "Personal goals changed; reload the current revision",
            Self::Database => "Personal goals could not be accessed",
        })
    }
}
impl Error for PersonalGoalsError {}
impl From<sqlx::Error> for PersonalGoalsError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Resolve the enrolled owner after an API credential has been authorized.
    pub async fn enrolled_owner_id(&self) -> Result<Uuid, PersonalGoalsError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(PersonalGoalsError::Storage)?;
            let owner = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
                .fetch_optional(&mut *txn)
                .await?
                .ok_or(PersonalGoalsError::Owner)?;
            txn.commit().await?;
            Ok(owner)
        })
        .await
        .map_err(|_| PersonalGoalsError::Database)?
    }

    /// Read the enrolled owner's current preferences without writing legacy rows.
    pub async fn personal_goals(&self, owner: Uuid) -> Result<PersonalGoals, PersonalGoalsError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
            prepare(&mut txn, owner).await?;
            let (goals, _) = load(&mut txn, owner).await?;
            txn.commit().await?;
            Ok(goals)
        })
        .await
        .map_err(|_| PersonalGoalsError::Database)?
    }

    /// Preserve the preferences endpoint's legacy owner binding on first access.
    pub async fn bound_personal_goals(
        &self,
        owner: Uuid,
    ) -> Result<PersonalGoals, PersonalGoalsError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin().await?;
            prepare(&mut txn, owner).await?;
            let (mut goals, stored) = load(&mut txn, owner).await?;
            if needs_owner(&stored) {
                lock_goals(&mut txn).await?;
                let (current, mut current_row) = load(&mut txn, owner).await?;
                goals = current;
                if needs_owner(&current_row) {
                    current_row["owner_id"] = json!(owner.to_string());
                    save(&mut txn, &current_row).await?;
                }
            }
            txn.commit().await?;
            Ok(goals)
        })
        .await
        .map_err(|_| PersonalGoalsError::Database)?
    }

    /// Apply an authenticated owner choice. Channel commands carry trusted ordering
    /// evidence, and direct API changes require the current stored revision.
    pub async fn select_personal_goals(
        &self,
        owner: Uuid,
        selection: GoalSelection<'_>,
    ) -> Result<PersonalGoals, PersonalGoalsError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin().await?;
            prepare(&mut txn, owner).await?;
            let result = set_in(&mut txn, owner, selection).await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| PersonalGoalsError::Database)?
    }
}

async fn prepare(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<(), PersonalGoalsError> {
    sqlx::query("SET LOCAL statement_timeout='750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn)
        .await
        .map_err(PersonalGoalsError::Storage)?;
    let actual: Option<Uuid> = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_optional(&mut **txn)
        .await?;
    if actual == Some(owner) {
        Ok(())
    } else {
        Err(PersonalGoalsError::Owner)
    }
}

pub(crate) async fn load(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<(PersonalGoals, Value), PersonalGoalsError> {
    let value: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(KEY)
            .fetch_optional(&mut **txn)
            .await?;
    let Some(value) = value else {
        return Ok((PersonalGoals::default(), json!({})));
    };
    let stored: Value =
        serde_json::from_str(&value).map_err(|_| PersonalGoalsError::InvalidState)?;
    if let Some(bound) = stored.get("owner_id").filter(|bound| !bound.is_null())
        && bound.as_str().and_then(|id| id.parse::<Uuid>().ok()) != Some(owner)
    {
        return Err(PersonalGoalsError::Owner);
    }
    let goals =
        serde_json::from_value(stored.clone()).map_err(|_| PersonalGoalsError::InvalidState)?;
    Ok((goals, stored))
}

#[expect(
    clippy::cast_precision_loss,
    reason = "The API change marker preserves the legacy fractional Unix seconds format"
)]
pub(crate) async fn set_in(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    selection: GoalSelection<'_>,
) -> Result<PersonalGoals, PersonalGoalsError> {
    if selection.goals.len() > 4
        || selection.command.is_some_and(|order| {
            order.tie_breaker.is_empty() || order.tie_breaker.chars().count() > 1000
        })
    {
        return Err(PersonalGoalsError::InvalidSelection);
    }
    let mut goals = selection.goals.to_vec();
    goals.sort_unstable();
    if goals.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(PersonalGoalsError::InvalidSelection);
    }
    lock_goals(txn).await?;
    let (current, mut stored) = load(txn, owner).await?;
    let bind_legacy = needs_owner(&stored);
    if bind_legacy {
        stored["owner_id"] = json!(owner.to_string());
    }
    if let Some(incoming) = selection.command
        && superseded(&stored, incoming)?
    {
        if bind_legacy {
            save(txn, &stored).await?;
        }
        return Ok(current);
    }
    if current.revision != selection.expected_revision {
        return Err(PersonalGoalsError::Conflict);
    }
    let order = selection.command.map(|input| {
        let mut time = input.received_at.to_string();
        time.truncate(time.len() - "+00:00".len());
        time.push('Z');
        json!({"source_epoch":input.source_epoch,"received_at":time,"tie_breaker":input.tie_breaker})
    });
    let now = selection.now.unix_microseconds() as f64 / 1_000_000.0;
    if current.configured && current.goals == goals {
        if let Some(order) = order {
            stored["command_order"] = order;
        } else {
            stored["api_changed_at"] = json!(now);
        }
        save(txn, &stored).await?;
        return Ok(current);
    }
    let old = serde_json::to_value(&current).map_err(|_| PersonalGoalsError::InvalidState)?;
    let mut history = match stored.get("history") {
        Some(Value::Array(history)) => history[history.len().saturating_sub(19)..].to_vec(),
        None => Vec::new(),
        _ => return Err(PersonalGoalsError::InvalidState),
    };
    history.push(old);
    let result = PersonalGoals {
        configured: true,
        revision: current
            .revision
            .checked_add(1)
            .ok_or(PersonalGoalsError::InvalidState)?,
        goals,
        updated_at: Some(selection.now.to_string()),
    };
    let mut value = serde_json::to_value(&result).map_err(|_| PersonalGoalsError::InvalidState)?;
    value["owner_id"] = json!(owner.to_string());
    value["history"] = json!(history);
    value["api_changed_at"] = if order.is_some() {
        stored["api_changed_at"].clone()
    } else {
        json!(now)
    };
    value["command_order"] = order.unwrap_or_else(|| stored["command_order"].clone());
    save(txn, &value).await?;
    Ok(result)
}

fn needs_owner(stored: &Value) -> bool {
    stored
        .as_object()
        .is_some_and(|row| !row.is_empty() && stored["owner_id"].is_null())
}

async fn lock_goals(txn: &mut Transaction<'_, Postgres>) -> Result<(), PersonalGoalsError> {
    for lock in [REPLAY_MUTATION_LOCK, GOAL_POLICY_LOCK] {
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(lock)
            .fetch_one(&mut **txn)
            .await?;
        if !locked {
            return Err(PersonalGoalsError::Busy);
        }
    }
    Ok(())
}

#[expect(
    clippy::cast_possible_truncation,
    clippy::cast_precision_loss,
    reason = "Order comparisons preserve the legacy whole and fractional Unix seconds format"
)]
fn superseded(stored: &Value, incoming: GoalCommandOrder<'_>) -> Result<bool, PersonalGoalsError> {
    if !stored["api_changed_at"].is_null() {
        let changed_at = stored["api_changed_at"]
            .as_f64()
            .ok_or(PersonalGoalsError::InvalidState)?;
        let epoch = changed_at as i64;
        if incoming.source_epoch < epoch
            || (incoming.source_epoch == epoch
                && incoming.received_at.unix_microseconds() as f64 / 1_000_000.0 <= changed_at)
        {
            return Ok(true);
        }
    }
    if let Some(previous) = stored
        .get("command_order")
        .filter(|previous| !previous.is_null())
    {
        let source = previous["source_epoch"]
            .as_i64()
            .ok_or(PersonalGoalsError::InvalidState)?;
        let received = previous["received_at"]
            .as_str()
            .and_then(|at| at.parse::<StoredInstant>().ok())
            .ok_or(PersonalGoalsError::InvalidState)?;
        let tie = previous["tie_breaker"]
            .as_str()
            .ok_or(PersonalGoalsError::InvalidState)?;
        let earlier = (incoming.source_epoch, incoming.received_at)
            .cmp(&(source, received))
            .then_with(|| compare_command_ids(incoming.tie_breaker, tie));
        return Ok(!earlier.is_gt());
    }
    if let Some(previous) = stored
        .get("telegram_order")
        .filter(|previous| !previous.is_null())
    {
        let source = previous[0]
            .as_i64()
            .ok_or(PersonalGoalsError::InvalidState)?;
        let id = match &previous[1] {
            Value::String(value) => value.clone(),
            Value::Number(value) => value.to_string(),
            _ => return Err(PersonalGoalsError::InvalidState),
        };
        return Ok(incoming.source_epoch < source
            || (incoming.source_epoch == source && incoming.tie_breaker == id));
    }
    Ok(false)
}

fn compare_command_ids(incoming: &str, previous: &str) -> std::cmp::Ordering {
    let numeric = |value: &str| {
        value
            .parse::<i64>()
            .ok()
            .filter(|id| *id > 0 && value.bytes().all(|digit| digit.is_ascii_digit()))
    };
    match (numeric(incoming), numeric(previous)) {
        (Some(incoming), Some(previous)) => incoming.cmp(&previous),
        _ => incoming.cmp(previous),
    }
}

async fn save(
    txn: &mut Transaction<'_, Postgres>,
    value: &Value,
) -> Result<(), PersonalGoalsError> {
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    .bind(KEY)
    .bind(value.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}
