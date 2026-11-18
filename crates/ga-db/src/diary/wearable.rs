use std::{error::Error, fmt};

use ga_domain::wearable::WearableMark;
use serde_json::{Value, json};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{CreateSystemEvent, DiaryWriteError, stored_instant};
use crate::Database;

#[derive(Debug)]
pub enum WearableError {
    InvalidBatch,
    ContentChanged,
    Diary(DiaryWriteError),
}

impl fmt::Display for WearableError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidBatch => f.write_str("Wearable marks are invalid"),
            Self::ContentChanged => {
                f.write_str("Wearable mark ID was already used with different content")
            }
            Self::Diary(error) => error.fmt(f),
        }
    }
}
impl Error for WearableError {}
impl From<DiaryWriteError> for WearableError {
    fn from(error: DiaryWriteError) -> Self {
        Self::Diary(error)
    }
}
impl From<sqlx::Error> for WearableError {
    fn from(error: sqlx::Error) -> Self {
        Self::Diary(error.into())
    }
}

impl Database {
    /// Commit the entire batch and its immutable receipts before acknowledging uploads.
    pub async fn accept_wearable_marks(
        &self,
        owner: Uuid,
        device: Uuid,
        marks: &[WearableMark],
    ) -> Result<Value, WearableError> {
        if !(1..=20).contains(&marks.len()) {
            return Err(WearableError::InvalidBatch);
        }
        let mut txn = self.begin_diary_write(owner).await?;
        let received_at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *txn)
            .await?;
        let received_at = stored_instant(received_at)?.to_string();
        let actor = format!("wearable:{device}");
        let mut acknowledgements = Vec::with_capacity(marks.len());
        for mark in marks {
            let id = mark.id();
            let key = format!("wearable-receipt:{device}:{id}");
            let previous: Option<Option<String>> =
                sqlx::query_scalar("SELECT value->>'hash' FROM app_state WHERE key=$1")
                    .bind(&key)
                    .fetch_optional(&mut *txn)
                    .await?;
            if let Some(previous) = previous {
                if previous.as_deref() != Some(mark.digest()) {
                    return Err(WearableError::ContentChanged);
                }
            } else {
                let idempotency = format!("{actor}:{id}");
                let stored = Self::create_system_event_in(
                    &mut txn,
                    CreateSystemEvent {
                        owner_id: owner,
                        authenticated_actor: &actor,
                        report: mark.report(),
                        idempotency_key: Some(&idempotency),
                        operation_id: None,
                        clock_uncertainty_seconds: mark.clock_uncertainty_seconds(),
                    },
                )
                .await?;
                let receipt = json!({
                    "hash": mark.digest(), "event_id": stored.id.to_string(),
                    "received_at": received_at, "device_time": mark.device_time(),
                    "timezone": mark.report().timezone(),
                    "clock_uncertainty_seconds": mark.clock_uncertainty_seconds(),
                    "time_status": "unverified_device_clock",
                });
                sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
                    .bind(key)
                    .bind(receipt.to_string())
                    .execute(&mut *txn)
                    .await?;
            }
            acknowledgements.push(json!({"id": id.to_string(), "accepted": true}));
        }
        txn.commit().await?;
        Ok(json!({"acknowledgements": acknowledgements}))
    }
}
