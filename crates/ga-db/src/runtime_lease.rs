use std::{error::Error, fmt, future::Future, time::Duration};

use sqlx::{Connection, PgConnection};
use tokio::time::Instant;

use crate::{Database, ReadinessError, check_readiness};

pub(crate) const RUNTIME_LOCK: i64 = 72104620;
const DATABASE_DEADLINE: Duration = Duration::from_millis(750);
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(1);
const WORKER_HEARTBEAT_INTERVAL: Duration = Duration::from_secs(30);

/// The Python runtime and the native worker use the same dedicated PostgreSQL lock.
pub struct RuntimeLease {
    pub(crate) connection: PgConnection,
    next_worker_heartbeat: Option<Instant>,
}

#[derive(Clone, Copy)]
pub struct RuntimeWorkerIdentity {
    pid: i32,
    backend_start: time::OffsetDateTime,
}

impl RuntimeWorkerIdentity {
    pub(crate) async fn still_held(
        self,
        connection: &mut PgConnection,
    ) -> Result<bool, sqlx::Error> {
        sqlx::query_scalar(
            "SELECT EXISTS(
                SELECT 1 FROM pg_locks lease JOIN pg_stat_activity active ON active.pid=lease.pid
                WHERE lease.pid=$1 AND active.backend_start=$2
                  AND lease.locktype='advisory' AND lease.objsubid=1
                  AND lease.mode='ExclusiveLock' AND lease.granted
                  AND lease.database=(SELECT oid FROM pg_database WHERE datname=current_database())
                  AND ((lease.classid::bigint<<32)|lease.objid::bigint)=$3
            )",
        )
        .bind(self.pid)
        .bind(self.backend_start)
        .bind(RUNTIME_LOCK)
        .fetch_one(connection)
        .await
    }
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
            Ok(RuntimeLease {
                connection,
                next_worker_heartbeat: None,
            })
        })
        .await
        .map_err(|_| RuntimeLeaseError::Database)?
    }

    pub async fn worker_identity_held(
        &self,
        worker: RuntimeWorkerIdentity,
    ) -> Result<bool, RuntimeLeaseError> {
        tokio::time::timeout(DATABASE_DEADLINE, async {
            let mut connection = self.control.acquire().await?;
            worker
                .still_held(&mut connection)
                .await
                .map_err(RuntimeLeaseError::from)
        })
        .await
        .map_err(|_| RuntimeLeaseError::Database)?
    }

    /// Observe a recent heartbeat from the same session that still owns this database's worker.
    pub async fn native_worker_healthy(&self) -> Result<bool, ReadinessError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
            sqlx::query("SET LOCAL statement_timeout = '750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn).await?;
            let alive = sqlx::query_scalar(
                "SELECT EXISTS(
                    SELECT FROM app_state AS pulse
                    JOIN pg_locks AS lease ON lease.pid=(pulse.value->>'native_pid')::int
                    JOIN pg_stat_activity AS session ON session.pid=lease.pid
                    WHERE pulse.key='runtime:heartbeat' AND lease.locktype='advisory'
                      AND lease.database=(SELECT oid FROM pg_database WHERE datname=current_database())
                      AND lease.objsubid=1 AND lease.granted AND lease.mode='ExclusiveLock'
                      AND ((lease.classid::bigint << 32) | lease.objid::bigint)=$1
                      AND session.backend_start=(pulse.value->>'native_backend_start')::timestamptz
                      AND (pulse.value->>'at')::timestamptz <= statement_timestamp()
                      AND (pulse.value->>'at')::timestamptz > statement_timestamp() - interval '180 seconds'
                )",
            )
            .bind(RUNTIME_LOCK)
            .fetch_one(&mut *txn)
            .await?;
            txn.rollback().await?;
            Ok(alive)
        })
        .await
        .map_err(|_| ReadinessError::Unavailable)?
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

    pub async fn worker_identity(&mut self) -> Result<RuntimeWorkerIdentity, RuntimeLeaseError> {
        let (pid, backend_start) = tokio::time::timeout(
            DATABASE_DEADLINE,
            sqlx::query_as(
                "SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()",
            )
            .fetch_one(&mut self.connection),
        )
        .await
        .map_err(|_| RuntimeLeaseError::Database)??;
        Ok(RuntimeWorkerIdentity { pid, backend_start })
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
        if self
            .next_worker_heartbeat
            .is_some_and(|next| next <= Instant::now())
        {
            return self.publish_worker_heartbeat().await;
        }
        tokio::time::timeout(DATABASE_DEADLINE, self.connection.ping())
            .await
            .map_err(|_| RuntimeLeaseError::Database)??;
        Ok(())
    }

    /// Publish liveness from the dedicated worker connection, retaining the legacy heartbeat time.
    pub async fn publish_worker_heartbeat(&mut self) -> Result<(), RuntimeLeaseError> {
        tokio::time::timeout(
            DATABASE_DEADLINE,
            sqlx::query(
                "INSERT INTO app_state(key,value)
                 SELECT 'runtime:heartbeat', jsonb_build_object(
                     'at',clock_timestamp(), 'native_pid',pid, 'native_backend_start',backend_start
                 ) FROM pg_stat_activity WHERE pid=pg_backend_pid()
                 ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            )
            .execute(&mut self.connection),
        )
        .await
        .map_err(|_| RuntimeLeaseError::Database)??;
        self.next_worker_heartbeat = Some(Instant::now() + WORKER_HEARTBEAT_INTERVAL);
        Ok(())
    }
}
