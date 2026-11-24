use std::{collections::BTreeMap, time::Duration};

use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::ReplaySelectionError;
use crate::{Database, check_readiness};

pub(crate) const REPLAY_MUTATION_LOCK: i64 = 72_104_619;

#[derive(Debug, serde::Serialize, Eq, PartialEq)]
pub struct ReplayStatus {
    pub ready: bool,
    pub target_parser_version: i32,
    pub jobs: BTreeMap<String, i64>,
    pub outcomes: BTreeMap<String, i64>,
}

impl Database {
    /// Read the actual projection generation status and durable progress from
    /// one committed snapshot. A timed-out scan is unavailable.
    pub async fn replay_status(
        &self,
        target_parser_version: i32,
    ) -> Result<ReplayStatus, ReplaySelectionError> {
        if target_parser_version < 1 {
            return Err(ReplaySelectionError::InvalidVersion);
        }
        let read = async {
            let mut txn = self
                .work
                .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                .await?;
            sqlx::query("SET LOCAL statement_timeout = '750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(ReplaySelectionError::Unavailable)?;
            let status = replay_status_in(&mut txn, target_parser_version).await?;
            txn.commit().await?;
            Ok(status)
        };
        tokio::time::timeout(Duration::from_millis(1500), read)
            .await
            .map_err(|_| ReplaySelectionError::Database)?
    }
}

pub(crate) async fn replay_status_in(
    txn: &mut Transaction<'_, Postgres>,
    target_parser_version: i32,
) -> Result<ReplayStatus, ReplaySelectionError> {
    let ready = !projection_pending(txn, target_parser_version).await?;
    let jobs: Vec<(String, i64)> = sqlx::query_as(
        "SELECT status, count(*) FROM jobs WHERE kind = 'raw_replay'
           AND (payload->>'target_version')::int = $1 GROUP BY status",
    )
    .bind(target_parser_version)
    .fetch_all(&mut **txn)
    .await?;
    let outcomes: Vec<(String, i64)> = sqlx::query_as(
        "SELECT value->>'status', count(*) FROM app_state
         WHERE starts_with(key, 'replay:')
           AND (value->>'target_version')::int = $1
         GROUP BY value->>'status'",
    )
    .bind(target_parser_version)
    .fetch_all(&mut **txn)
    .await?;
    Ok(ReplayStatus {
        ready,
        target_parser_version,
        jobs: jobs.into_iter().collect(),
        outcomes: outcomes.into_iter().collect(),
    })
}

pub(crate) async fn projection_pending(
    txn: &mut Transaction<'_, Postgres>,
    target: i32,
) -> Result<bool, sqlx::Error> {
    // Use the scheduler's canonical predicate. No rows or raw payloads
    // leave Postgres, and a large candidate set can spill within work_mem.
    sqlx::query_scalar(concat!(
        "SELECT EXISTS (SELECT 1 FROM (",
        include_str!("selection.sql"),
        ") AS selection WHERE canonical)"
    ))
    .bind(target)
    .bind(Option::<Uuid>::None)
    .bind(Option::<i64>::None)
    .bind(Option::<&[Uuid]>::None)
    .bind(false)
    .fetch_one(&mut **txn)
    .await
}
