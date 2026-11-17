use std::{collections::BTreeMap, error::Error, fmt, time::Duration};

mod onboarding;

use ga_domain::{
    model_gate::{ModelGateIdentity, ModelGateState, PRIMARY_GEMINI_STATE},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Connection, PgConnection, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness};

#[derive(Debug)]
pub enum ModelGateError {
    Storage(ReadinessError),
    Busy,
    Database,
}

impl fmt::Display for ModelGateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Model state storage is unavailable",
            Self::Busy => "A request for this model integration is already in progress",
            Self::Database => "Saved model state could not be accessed",
        })
    }
}
impl Error for ModelGateError {}

impl From<sqlx::Error> for ModelGateError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

/// A dedicated PostgreSQL session serializing work with the pinned Python provider.
/// Dropping the session releases its advisory lock, including on cancellation.
pub struct ModelGateSession<'a> {
    identity: &'a ModelGateIdentity,
    connection: PgConnection,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum ModelGateOutcomeReason {
    Ready,
    Quota,
    Auth,
    Model,
    ModelCooldown,
    Unavailable,
}

impl ModelGateOutcomeReason {
    fn label(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::Quota => "quota",
            Self::Auth => "auth",
            Self::Model => "model",
            Self::ModelCooldown => "model_cooldown",
            Self::Unavailable => "unavailable",
        }
    }
}

pub struct RecordModelGate<'a> {
    pub now: StoredInstant,
    pub reason: ModelGateOutcomeReason,
    pub blocked_until: Option<StoredInstant>,
    pub scheduler_pause_until: Option<StoredInstant>,
    pub model_cooldowns: &'a BTreeMap<String, StoredInstant>,
    pub notify_quota: bool,
}

impl Database {
    pub async fn read_model_gate(
        &self,
        identity: &ModelGateIdentity,
        now: StoredInstant,
    ) -> Result<ModelGateState, ModelGateError> {
        let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
        prepare(&mut txn).await?;
        let value = read_value(&mut txn, identity).await?;
        txn.commit().await?;
        Ok(ModelGateState::read(&value, identity, now))
    }

    /// Acquire the exact provider advisory lock without holding a transaction during a request.
    pub async fn lock_model_gate<'a>(
        &self,
        identity: &'a ModelGateIdentity,
    ) -> Result<ModelGateSession<'a>, ModelGateError> {
        let options = self.control.connect_options();
        let mut connection = tokio::time::timeout(
            Duration::from_millis(750),
            PgConnection::connect_with(options.as_ref()),
        )
        .await
        .map_err(|_| ModelGateError::Database)??;
        sqlx::query("SET statement_timeout = '750ms'")
            .execute(&mut connection)
            .await?;
        let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock($1)")
            .bind(identity.lock_key())
            .fetch_one(&mut connection)
            .await?;
        if !acquired {
            return Err(ModelGateError::Busy);
        }
        Ok(ModelGateSession {
            identity,
            connection,
        })
    }
}

impl ModelGateSession<'_> {
    pub async fn read(&mut self, now: StoredInstant) -> Result<ModelGateState, ModelGateError> {
        let mut txn = self.connection.begin_with("BEGIN READ ONLY").await?;
        prepare(&mut txn).await?;
        let value = read_value(&mut txn, self.identity).await?;
        txn.commit().await?;
        Ok(ModelGateState::read(&value, self.identity, now))
    }

    /// Store a model result and, when enabled, its quota notice in the same transaction.
    /// Persistence errors do not imply that the upstream request can be sent again.
    pub async fn record(&mut self, outcome: RecordModelGate<'_>) -> Result<(), ModelGateError> {
        let mut txn = self.connection.begin().await?;
        prepare(&mut txn).await?;
        let prior = read_value(&mut txn, self.identity).await?;
        let prior_pause = prior["scheduler_pause_until"]
            .as_str()
            .filter(|_| prior["configuration"].as_str() == Some(self.identity.configuration()))
            .and_then(|text| Some((text, text.parse::<StoredInstant>().ok()?)))
            .filter(|(_, at)| *at > outcome.now);
        let scheduler_pause = match prior_pause {
            Some((text, previous))
                if outcome
                    .scheduler_pause_until
                    .is_none_or(|until| previous > until) =>
            {
                Some(text.to_owned())
            }
            _ => outcome.scheduler_pause_until.map(|at| at.to_string()),
        };
        let model_cooldowns: BTreeMap<_, _> = outcome
            .model_cooldowns
            .iter()
            .map(|(model, at)| (model, at.to_string()))
            .collect();
        let mut value = json!({
            "configuration": self.identity.configuration(),
            "reason": outcome.reason.label(),
            "blocked_until": outcome.blocked_until.map(|at| at.to_string()),
            "scheduler_pause_until": scheduler_pause,
            "model_cooldowns": model_cooldowns,
            "at": outcome.now.to_string(),
        });
        if self.identity.state_key() == PRIMARY_GEMINI_STATE
            && (prior_pause.is_none() || prior.get("instance_id").is_some())
        {
            value["instance_id"] = json!(ga_domain::model_gate::PRIMARY_GEMINI_INSTANCE);
        }
        let gemini = self.identity.state_key() == PRIMARY_GEMINI_STATE
            || self.identity.inherits_legacy_gemini_state();
        if gemini && outcome.notify_quota && outcome.reason == ModelGateOutcomeReason::Quota {
            let now = OffsetDateTime::from(outcome.now);
            let key = format!("quota:{}-{:02}", now.date(), now.hour());
            sqlx::query(
                "INSERT INTO jobs (id,kind,payload,dedup_key,status,run_at,attempts)
                 VALUES ($1,'telegram_provider_notice',$2::jsonb,$3,'pending',$4,0)
                 ON CONFLICT (dedup_key) DO NOTHING",
            )
            .bind(Uuid::new_v4())
            .bind(json!({"outbox_key": key}).to_string())
            .bind(format!("provider-notice:{key}"))
            .bind(now)
            .execute(&mut *txn)
            .await?;
        }
        sqlx::query(
            "INSERT INTO app_state (key, value) VALUES ($1, $2::jsonb)
             ON CONFLICT (key) DO UPDATE SET value=excluded.value",
        )
        .bind(self.identity.state_key())
        .bind(value.to_string())
        .execute(&mut *txn)
        .await?;
        txn.commit().await?;
        Ok(())
    }
}

async fn prepare(txn: &mut Transaction<'_, Postgres>) -> Result<(), ModelGateError> {
    sqlx::query("SET LOCAL statement_timeout = '750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn).await.map_err(ModelGateError::Storage)
}

async fn read_value(
    txn: &mut Transaction<'_, Postgres>,
    identity: &ModelGateIdentity,
) -> Result<Value, ModelGateError> {
    let value: Option<String> = sqlx::query_scalar(
        "SELECT value::text FROM app_state
         WHERE key = $1 OR
             ($2 AND key = $3 AND NOT (jsonb_typeof(value) = 'object' AND value ? 'instance_id'))
         ORDER BY (key = $1) DESC
         LIMIT 1",
    )
    .bind(identity.state_key())
    .bind(identity.inherits_legacy_gemini_state())
    .bind(PRIMARY_GEMINI_STATE)
    .fetch_optional(&mut **txn)
    .await?;
    value
        .as_deref()
        .map_or(Ok(Value::Null), serde_json::from_str)
        .map_err(|_| ModelGateError::Database)
}
