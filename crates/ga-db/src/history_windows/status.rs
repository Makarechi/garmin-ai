use std::time::Duration;

use serde_json::Value;
use sqlx::{Postgres, Transaction};

use super::HistoryScheduleError;
use crate::{Database, check_readiness};

impl Database {
    /// Describe the durable history queue and scans in one committed read.
    pub async fn history_status(&self) -> Result<Value, HistoryScheduleError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self
                .work
                .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                .await?;
            sqlx::query("SET LOCAL statement_timeout = '750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(HistoryScheduleError::Storage)?;
            let status = history_status_in(&mut txn).await?;
            txn.commit().await?;
            Ok(status)
        })
        .await
        .map_err(|_| HistoryScheduleError::Database)?
    }
}

pub(crate) async fn history_status_in(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<Value, HistoryScheduleError> {
    let document: String = sqlx::query_scalar(include_str!("status.sql"))
        .fetch_one(&mut **txn)
        .await?;
    serde_json::from_str(&document).map_err(|_| HistoryScheduleError::InvalidState)
}
