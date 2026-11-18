use std::{error::Error, fmt, future::Future, time::Duration};

use sqlx::{Connection, PgConnection};

use crate::{Database, ReadinessError};

pub(crate) const RUNTIME_LOCK: i64 = 72104620;
const DATABASE_DEADLINE: Duration = Duration::from_millis(750);
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(1);

/// The Python runtime and the native worker use the same dedicated PostgreSQL lock.
pub struct RuntimeLease {
    pub(crate) connection: PgConnection,
}

#[derive(Debug)]
pub enum RuntimeLeaseError {
    Occupied,
    Readiness(ReadinessError),
    Database,
}

impl fmt::Display for RuntimeLeaseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Occupied => "Another Garmin AI worker is already running",
            Self::Readiness(_) => "Garmin AI worker storage is unavailable",
            Self::Database => "Garmin AI worker lost its runtime connection",
        })
    }
}
impl Error for RuntimeLeaseError {}

impl From<sqlx::Error> for RuntimeLeaseError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    pub async fn acquire_runtime_lease(&self) -> Result<RuntimeLease, RuntimeLeaseError> {
        self.readiness()
            .await
            .map_err(RuntimeLeaseError::Readiness)?;
        let options = self.control.connect_options();
        tokio::time::timeout(DATABASE_DEADLINE, async {
            let mut connection = PgConnection::connect_with(options.as_ref()).await?;
            let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock($1)")
                .bind(RUNTIME_LOCK)
                .fetch_one(&mut connection)
                .await?;
            if !acquired {
                return Err(RuntimeLeaseError::Occupied);
            }
            Ok(RuntimeLease { connection })
        })
        .await
        .map_err(|_| RuntimeLeaseError::Database)?
    }
}

impl RuntimeLease {
    /// Keep verifying runtime ownership during waits that do not borrow its connection.
    pub async fn while_held<F: Future>(
        &mut self,
        operation: F,
    ) -> Result<F::Output, RuntimeLeaseError> {
        let mut heartbeat = tokio::time::interval(HEARTBEAT_INTERVAL);
        heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        tokio::pin!(operation);
        loop {
            tokio::select! {
                biased;
                _ = heartbeat.tick() => self.check().await?,
                result = &mut operation => return Ok(result),
            }
        }
    }

    /// Recorded onboarding preferences take precedence over bootstrap configuration.
    pub async fn owner_timezone(&mut self) -> Result<Option<String>, RuntimeLeaseError> {
        let result = tokio::time::timeout(
            DATABASE_DEADLINE,
            sqlx::query_scalar(
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM app_state WHERE key='preferences:onboarding')
                         THEN (SELECT timezone FROM people) END",
            )
            .fetch_one(&mut self.connection),
        )
        .await
        .map_err(|_| RuntimeLeaseError::Database)??;
        Ok(result)
    }

    pub async fn check(&mut self) -> Result<(), RuntimeLeaseError> {
        tokio::time::timeout(DATABASE_DEADLINE, self.connection.ping())
            .await
            .map_err(|_| RuntimeLeaseError::Database)??;
        Ok(())
    }
}
