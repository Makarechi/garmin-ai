use std::{error::Error, fmt};

use ga_domain::{
    model_gate::{ModelGateIdentity, ModelGateState, PRIMARY_GEMINI_STATE},
    timestamp::StoredInstant,
};
use serde_json::Value;

use crate::{Database, ReadinessError, check_readiness};

#[derive(Debug)]
pub enum ModelGateError {
    Storage(ReadinessError),
    Database,
}

impl fmt::Display for ModelGateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Model state storage is unavailable",
            Self::Database => "Saved model state could not be read",
        })
    }
}
impl Error for ModelGateError {}

impl From<sqlx::Error> for ModelGateError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    pub async fn read_model_gate(
        &self,
        identity: &ModelGateIdentity,
        now: StoredInstant,
    ) -> Result<ModelGateState, ModelGateError> {
        let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(ModelGateError::Storage)?;
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
        .fetch_optional(&mut *txn)
        .await?;
        txn.commit().await?;
        let value: Value = value
            .as_deref()
            .map(serde_json::from_str)
            .transpose()
            .map_err(|_| ModelGateError::Database)?
            .unwrap_or(Value::Null);
        Ok(ModelGateState::read(&value, identity, now))
    }
}
