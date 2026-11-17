use std::{error::Error, fmt};

use ga_domain::{event_input::source_calendar_date, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

use crate::{
    Database, ReadinessError,
    activity_documents::put_state,
    check_readiness,
    garmin_owner::{OwnerVerificationError, verify_owner_in_transaction},
};

const MUTATION_LOCK: i64 = 72104619;
const RECENT_DAYS: u16 = 14;
const MAX_HORIZON: u16 = 3660;

pub struct StartActivityScans<'a> {
    pub authenticated_fingerprint: &'a str,
    pub timezone: &'a str,
    pub history_days: u16,
    pub now: StoredInstant,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActivityScansStarted {
    Busy,
    CollectionDisabled,
    Scheduled { pages: usize },
}

#[derive(Debug)]
pub enum ActivityScanError {
    Storage(ReadinessError),
    Owner(OwnerVerificationError),
    InvalidHorizon,
    InvalidState,
    Database,
}

impl fmt::Display for ActivityScanError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Activity scan storage is unavailable",
            Self::Owner(_) => "Garmin activity scan owner could not be verified",
            Self::InvalidHorizon => "Activity scan horizon or timezone is invalid",
            Self::InvalidState => "Saved activity scan state is invalid",
            Self::Database => "Activity scan could not be scheduled",
        })
    }
}
impl Error for ActivityScanError {}

impl From<sqlx::Error> for ActivityScanError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Create missing recent and history scans without replacing any active cursor.
    pub async fn start_activity_scans(
        &self,
        request: StartActivityScans<'_>,
    ) -> Result<ActivityScansStarted, ActivityScanError> {
        if request.history_days > MAX_HORIZON {
            return Err(ActivityScanError::InvalidHorizon);
        }
        let today = source_calendar_date(request.now, request.timezone)
            .ok_or(ActivityScanError::InvalidHorizon)?;
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(ActivityScanError::Storage)?;
        let owner = verify_owner_in_transaction(&mut txn, request.authenticated_fingerprint)
            .await
            .map_err(ActivityScanError::Owner)?;
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !locked {
            return Ok(ActivityScansStarted::Busy);
        }
        let enabled: bool = sqlx::query_scalar(
            "SELECT NOT EXISTS (SELECT 1 FROM module_configs WHERE owner_id=$1)
             OR EXISTS (SELECT 1 FROM module_configs WHERE owner_id=$1 AND pack_key='training'
                        AND collection_enabled)",
        )
        .bind(owner.owner_id())
        .fetch_one(&mut *txn)
        .await?;
        if !enabled {
            return Ok(ActivityScansStarted::CollectionDisabled);
        }

        let account = request.authenticated_fingerprint;
        let mut pages = 0;
        for (lane, days) in [("recent", RECENT_DAYS), ("history", request.history_days)] {
            if days == 0 {
                continue;
            }
            let scan_key = format!("activity_scan:{account}:{lane}");
            if !scan_is_due(&mut txn, &scan_key, request.now).await? {
                continue;
            }
            let since = today
                .checked_sub(Duration::days(i64::from(days)))
                .ok_or(ActivityScanError::InvalidHorizon)?;
            let generation = Uuid::new_v4().to_string();
            let job = json!({
                "generation": generation, "scan_key": scan_key, "account": account,
                "offset": 0, "round": 0, "anchor": [], "backfill": lane == "history",
                "since": since.to_string(),
            });
            let id = enqueue_page(
                &mut txn,
                &job,
                &format!("activity-page:{account}:{generation}:0:0"),
                request.now,
            )
            .await?;
            let mut state = job;
            state["status"] = json!("running");
            state["job_id"] = json!(id.to_string());
            state["started_at"] = json!(request.now.to_string());
            state["pages"] = json!(0);
            put_state(&mut txn, &scan_key, &state).await?;
            pages += 1;
        }
        txn.commit().await?;
        Ok(ActivityScansStarted::Scheduled { pages })
    }
}

async fn scan_is_due(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    now: StoredInstant,
) -> Result<bool, ActivityScanError> {
    let state: Option<(Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT value->>'status', value->>'next_scan_at' FROM app_state WHERE key=$1",
    )
    .bind(key)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((status, next)) = state else {
        return Ok(true);
    };
    if status.as_deref() == Some("running") {
        return Ok(false);
    }
    let next: Option<StoredInstant> = next
        .filter(|time| !time.is_empty())
        .map(|time| time.parse().map_err(|_| ActivityScanError::InvalidState))
        .transpose()?;
    Ok(next.is_none_or(|time| time <= now))
}

async fn enqueue_page(
    txn: &mut Transaction<'_, Postgres>,
    payload: &Value,
    dedup: &str,
    now: StoredInstant,
) -> Result<Uuid, ActivityScanError> {
    let result = sqlx::query_scalar(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES ($1,'garmin_activities',$2::jsonb,$3,'pending',$4,0)
         ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
    )
    .bind(Uuid::new_v4())
    .bind(payload.to_string())
    .bind(dedup)
    .bind(OffsetDateTime::from(now))
    .fetch_optional(&mut **txn)
    .await?;
    result.ok_or(ActivityScanError::InvalidState)
}
