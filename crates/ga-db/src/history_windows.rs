use std::{collections::BTreeMap, error::Error, fmt};

use ga_domain::{event_input::source_calendar_date, sha256_hex, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, Duration, OffsetDateTime, format_description::well_known::Iso8601};
use uuid::Uuid;

use crate::{
    Database, ReadinessError,
    activity_documents::put_state,
    check_readiness,
    garmin_owner::{OwnerVerificationError, verify_owner_in_transaction},
    raw_capture::required_packs,
};

const MUTATION_LOCK: i64 = 72104619;
const MAX_HORIZON: u16 = 3660;
const SCAN_DAYS: i64 = 3661;
const DAYS_PER_PASS: i64 = 2;

const DAY_ENDPOINTS: &[&str] = &[
    "all_day_events",
    "body_battery",
    "body_battery_events",
    "body_composition",
    "daily",
    "endurance",
    "heart_rate",
    "hill",
    "hrv",
    "hydration",
    "intensity",
    "max_metrics",
    "readiness",
    "respiration",
    "resting_hr",
    "sleep",
    "spo2",
    "steps",
    "stress",
    "training_status",
];

#[derive(Clone, Copy)]
pub struct ScheduleHistory<'a> {
    pub authenticated_fingerprint: &'a str,
    pub timezone: &'a str,
    pub horizon_days: u16,
    pub now: StoredInstant,
}

#[derive(Debug, Default, Clone, Copy, Eq, PartialEq)]
pub struct HistorySchedule {
    pub queued: usize,
    pub reenabled: usize,
    pub scheduling_complete: bool,
}

#[derive(Debug)]
pub enum HistoryScheduleError {
    Storage(ReadinessError),
    Owner(OwnerVerificationError),
    InvalidRange,
    InvalidState,
    Database,
}

impl fmt::Display for HistoryScheduleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "History scheduling storage is unavailable",
            Self::Owner(_) => "History scheduling owner could not be verified",
            Self::InvalidRange => "Garmin history horizon or local date is invalid",
            Self::InvalidState => "Saved Garmin history cursor is invalid",
            Self::Database => "Garmin history windows could not be scheduled",
        })
    }
}
impl Error for HistoryScheduleError {}

impl From<sqlx::Error> for HistoryScheduleError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Advance the owner's durable daily history and enqueue new windows atomically.
    pub async fn schedule_history(
        &self,
        request: ScheduleHistory<'_>,
    ) -> Result<HistorySchedule, HistoryScheduleError> {
        if request.horizon_days == 0 {
            return Ok(HistorySchedule::default());
        }
        if request.horizon_days > MAX_HORIZON {
            return Err(HistoryScheduleError::InvalidRange);
        }
        let yesterday = source_calendar_date(request.now, request.timezone)
            .and_then(Date::previous_day)
            .ok_or(HistoryScheduleError::InvalidRange)?;
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(HistoryScheduleError::Storage)?;
        let owner = verify_owner_in_transaction(&mut txn, request.authenticated_fingerprint)
            .await
            .map_err(HistoryScheduleError::Owner)?;
        let allowed: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !allowed {
            return Ok(HistorySchedule::default());
        }
        let endpoints = enabled_endpoints(&mut txn, owner.owner_id()).await?;
        if endpoints.is_empty() {
            return Ok(HistorySchedule::default());
        }
        let prefix = format!("syncwindow:{}:", request.authenticated_fingerprint);
        let scope = Scope {
            prefix: &prefix,
            account: request.authenticated_fingerprint,
            endpoints: &endpoints,
            now: request.now,
        };
        let reenabled = retry_disabled(&mut txn, &scope).await?;
        let mut progress =
            schedule_windows(&mut txn, &scope, yesterday, request.horizon_days).await?;
        progress.reenabled = reenabled;
        txn.commit().await?;
        Ok(progress)
    }
}

async fn enabled_endpoints(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<Vec<&'static str>, HistoryScheduleError> {
    let rows: Vec<(String, bool)> =
        sqlx::query_as("SELECT pack_key, collection_enabled FROM module_configs WHERE owner_id=$1")
            .bind(owner)
            .fetch_all(&mut **txn)
            .await?;
    let choices: BTreeMap<_, _> = rows.into_iter().collect();
    if choices.is_empty() {
        return Ok(DAY_ENDPOINTS.to_vec());
    }
    Ok(DAY_ENDPOINTS
        .iter()
        .copied()
        .filter(|endpoint| {
            required_packs(endpoint)
                .is_some_and(|packs| packs.iter().all(|pack| choices.get(*pack) == Some(&true)))
        })
        .collect())
}

struct Scope<'a> {
    prefix: &'a str,
    account: &'a str,
    endpoints: &'a [&'static str],
    now: StoredInstant,
}

async fn retry_disabled(
    txn: &mut Transaction<'_, Postgres>,
    scope: &Scope<'_>,
) -> Result<usize, HistoryScheduleError> {
    let disabled: Vec<(String, String)> = sqlx::query_as(
        "SELECT key,value::text FROM app_state WHERE starts_with(key,$1)
           AND value->>'status'='disabled' AND value->>'endpoint'=ANY($2)
         ORDER BY key LIMIT 2",
    )
    .bind(scope.prefix)
    .bind(scope.endpoints)
    .fetch_all(&mut **txn)
    .await?;
    let count = disabled.len();
    for (key, payload) in disabled {
        let mut payload: Value =
            serde_json::from_str(&payload).map_err(|_| HistoryScheduleError::InvalidState)?;
        let endpoint = payload["endpoint"]
            .as_str()
            .ok_or(HistoryScheduleError::InvalidState)?;
        let day = stored_date(&payload["date"])?;
        let retry = format!("{key}:retry:{}", Uuid::new_v4());
        let job = enqueue(txn, scope, endpoint, day, &key, &retry).await?;
        payload["status"] = json!("pending");
        payload["job_id"] = json!(job.map(|id| id.to_string()));
        put_state(txn, &key, &payload).await?;
    }
    Ok(count)
}

async fn schedule_windows(
    txn: &mut Transaction<'_, Postgres>,
    scope: &Scope<'_>,
    yesterday: Date,
    horizon: u16,
) -> Result<HistorySchedule, HistoryScheduleError> {
    let digest = sha256_hex(scope.endpoints.join("\n").as_bytes());
    let generation = &digest[..16];
    let key = format!("syncplan:daily:{}:{horizon}:{generation}", scope.account);
    let saved: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(&key)
            .fetch_optional(&mut **txn)
            .await?;
    let mut plan: Value = match saved {
        Some(ref saved) => {
            serde_json::from_str(saved).map_err(|_| HistoryScheduleError::InvalidState)?
        }
        None => {
            let start = yesterday
                .checked_sub(Duration::days(i64::from(horizon) - 1))
                .ok_or(HistoryScheduleError::InvalidRange)?;
            json!({
                "account": scope.account, "history_start": start.to_string(),
                "history_next": yesterday.to_string(), "recent_through": yesterday.to_string(),
                "created_at": scope.now.to_string(), "horizon_days": horizon,
                "endpoint_generation": generation,
            })
        }
    };
    let mut recent = stored_date(&plan["recent_through"])?;
    let mut cursor = stored_date(&plan["history_next"])?;
    let left = stored_date(&plan["history_start"])?;
    if plan["scheduling_complete"] == true && recent >= yesterday {
        return Ok(HistorySchedule {
            scheduling_complete: true,
            ..HistorySchedule::default()
        });
    }

    let mut budget = DAYS_PER_PASS;
    let mut queued = 0;
    if recent < yesterday {
        let start = recent
            .next_day()
            .ok_or(HistoryScheduleError::InvalidRange)?;
        let phase = advance(txn, scope, start, yesterday, 1, budget).await?;
        recent = phase.last;
        budget -= phase.used;
        queued += phase.queued;
    }
    if cursor >= left && budget > 0 {
        let phase = advance(txn, scope, cursor, left, -1, budget).await?;
        cursor = phase
            .last
            .previous_day()
            .ok_or(HistoryScheduleError::InvalidRange)?;
        queued += phase.queued;
    }
    let scheduling_complete = cursor < left && recent == yesterday;
    plan["recent_through"] = json!(recent.to_string());
    plan["history_next"] = json!(cursor.to_string());
    plan["scheduling_complete"] = json!(scheduling_complete);
    put_state(txn, &key, &plan).await?;
    Ok(HistorySchedule {
        queued,
        reenabled: 0,
        scheduling_complete,
    })
}

struct Advance {
    last: Date,
    used: i64,
    queued: usize,
}

async fn advance(
    txn: &mut Transaction<'_, Postgres>,
    scope: &Scope<'_>,
    first: Date,
    final_day: Date,
    direction: i32,
    budget: i64,
) -> Result<Advance, HistoryScheduleError> {
    let scan = ((final_day - first).whole_days().abs() + 1).min(SCAN_DAYS);
    let days: Vec<Date> = sqlx::query_scalar(
        "SELECT $1::date + offset_by * $2 FROM generate_series(0,$3::int-1) AS offset_by
         WHERE EXISTS (
             SELECT 1 FROM unnest($4::text[]) AS endpoint
             WHERE NOT EXISTS (
                 SELECT 1 FROM app_state
                 WHERE key=concat($5::text,endpoint,':',to_char($1::date+offset_by*$2,'YYYY-MM-DD'))
             )
         )
         ORDER BY offset_by LIMIT $6",
    )
    .bind(first)
    .bind(direction)
    .bind(i32::try_from(scan).map_err(|_| HistoryScheduleError::InvalidRange)?)
    .bind(scope.endpoints)
    .bind(scope.prefix)
    .bind(budget)
    .fetch_all(&mut **txn)
    .await?;
    let used = i64::try_from(days.len()).map_err(|_| HistoryScheduleError::InvalidState)?;
    let last = if used == budget {
        *days.last().ok_or(HistoryScheduleError::InvalidState)?
    } else {
        first
            .checked_add(Duration::days((scan - 1) * i64::from(direction)))
            .ok_or(HistoryScheduleError::InvalidRange)?
    };
    let mut queued = 0;
    for day in days {
        queued += schedule_day(txn, scope, day).await?;
    }
    Ok(Advance { last, used, queued })
}

async fn schedule_day(
    txn: &mut Transaction<'_, Postgres>,
    scope: &Scope<'_>,
    day: Date,
) -> Result<usize, HistoryScheduleError> {
    let queued: i64 = sqlx::query_scalar(
        "WITH missing AS MATERIALIZED (
             SELECT endpoint,concat($2::text,endpoint,':',$3::text) AS window_key
             FROM unnest($1::text[]) AS endpoint
             WHERE NOT EXISTS (
                 SELECT 1 FROM app_state WHERE key=concat($2::text,endpoint,':',$3::text)
             )
         ), jobs_created AS (
             INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
             SELECT gen_random_uuid(),'garmin_endpoint',
                 jsonb_build_object('endpoint',endpoint,'key',$3,'backfill',true,
                     'account',$4::text,'sync_window',window_key),
                 window_key,'pending',$5,0
             FROM missing ON CONFLICT (dedup_key) DO NOTHING RETURNING id,dedup_key
         ), windows_created AS (
             INSERT INTO app_state(key,value)
             SELECT missing.window_key,jsonb_build_object('endpoint',endpoint,'date',$3,
                     'status','pending','job_id',jobs_created.id,'account',$4)
             FROM missing LEFT JOIN jobs_created ON jobs_created.dedup_key=missing.window_key
         )
         SELECT count(*) FROM jobs_created",
    )
    .bind(scope.endpoints)
    .bind(scope.prefix)
    .bind(day.to_string())
    .bind(scope.account)
    .bind(OffsetDateTime::from(scope.now))
    .fetch_one(&mut **txn)
    .await?;
    usize::try_from(queued).map_err(|_| HistoryScheduleError::InvalidState)
}

async fn enqueue(
    txn: &mut Transaction<'_, Postgres>,
    scope: &Scope<'_>,
    endpoint: &str,
    day: Date,
    window: &str,
    dedup: &str,
) -> Result<Option<Uuid>, sqlx::Error> {
    let payload = json!({
        "endpoint": endpoint, "key": day.to_string(), "backfill": true,
        "account": scope.account, "sync_window": window,
    });
    sqlx::query_scalar(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES ($1,'garmin_endpoint',$2::jsonb,$3,'pending',$4,0)
         ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
    )
    .bind(Uuid::new_v4())
    .bind(payload.to_string())
    .bind(dedup)
    .bind(OffsetDateTime::from(scope.now))
    .fetch_optional(&mut **txn)
    .await
}

fn stored_date(value: &Value) -> Result<Date, HistoryScheduleError> {
    value
        .as_str()
        .and_then(|date| Date::parse(date, &Iso8601::DEFAULT).ok())
        .ok_or(HistoryScheduleError::InvalidState)
}
