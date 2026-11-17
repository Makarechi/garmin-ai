mod repair;

use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{GARMIN_PARSER_VERSION, REPLAY_MUTATION_LOCK, ReplaySelectionError, source_page};
use crate::{
    Database, check_readiness,
    garmin_owner::{stored_garmin_fingerprint, verify_owner_in_transaction},
};

const MAX_QUEUE: i64 = 100;
const MAX_SCHEDULE: i64 = 25;
const SOURCE_CURSOR_KEY: &str = "replay:source_scan_cursor";

pub struct ScheduleReplay<'a> {
    pub authenticated_fingerprint: &'a str,
    pub target_parser_version: i32,
    pub now: StoredInstant,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ReplaySchedule {
    pub queued: usize,
    pub repaired: usize,
    pub scan_incomplete: bool,
    postponed: bool,
}

impl ReplaySchedule {
    pub fn plan_again_soon(self) -> bool {
        self.scan_incomplete || self.postponed
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Phase {
    Canonical,
    Superseded,
}

impl Phase {
    fn read(value: &Value) -> Self {
        if value["phase"] == "superseded" {
            Self::Superseded
        } else {
            Self::Canonical
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Canonical => "canonical",
            Self::Superseded => "superseded",
        }
    }
}

impl Database {
    /// Schedule bounded offline replay, atomically recording progress and updates to old jobs.
    pub async fn schedule_replay(
        &self,
        request: ScheduleReplay<'_>,
    ) -> Result<ReplaySchedule, ReplaySelectionError> {
        let target = request.target_parser_version;
        if target < 1 {
            return Err(ReplaySelectionError::InvalidVersion);
        }
        let mut txn = begin_planning(self).await?;
        verify_owner_in_transaction(&mut txn, request.authenticated_fingerprint)
            .await
            .map_err(ReplaySelectionError::Owner)?;
        let result = schedule_in(&mut txn, request).await?;
        txn.commit().await?;
        Ok(result)
    }

    /// Schedule archive maintenance from the already enrolled identity without Garmin access.
    pub async fn schedule_bound_replay(
        &self,
        now: StoredInstant,
    ) -> Result<ReplaySchedule, ReplaySelectionError> {
        let mut txn = begin_planning(self).await?;
        let Some(fingerprint) = stored_garmin_fingerprint(&mut txn)
            .await
            .map_err(ReplaySelectionError::Owner)?
        else {
            return Ok(ReplaySchedule::default());
        };
        let result = schedule_in(
            &mut txn,
            ScheduleReplay {
                authenticated_fingerprint: &fingerprint,
                target_parser_version: GARMIN_PARSER_VERSION,
                now,
            },
        )
        .await?;
        txn.commit().await?;
        Ok(result)
    }
}

async fn begin_planning(
    db: &Database,
) -> Result<Transaction<'static, Postgres>, ReplaySelectionError> {
    let mut txn = db.work.begin().await?;
    sqlx::query("SET LOCAL statement_timeout = '750ms'")
        .execute(&mut *txn)
        .await?;
    check_readiness(&mut txn)
        .await
        .map_err(ReplaySelectionError::Unavailable)?;
    Ok(txn)
}

async fn schedule_in(
    txn: &mut Transaction<'_, Postgres>,
    request: ScheduleReplay<'_>,
) -> Result<ReplaySchedule, ReplaySelectionError> {
    let target = request.target_parser_version;
    let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !acquired {
        return Ok(ReplaySchedule {
            postponed: true,
            ..ReplaySchedule::default()
        });
    }

    let queued: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM jobs WHERE kind = 'raw_replay' AND status IN ('pending','running')
           AND payload->>'target_version' = $1::int::text",
    )
    .bind(target)
    .fetch_one(&mut **txn)
    .await?;
    let capacity = usize::try_from((MAX_QUEUE - queued).clamp(0, MAX_SCHEDULE))
        .map_err(|_| ReplaySelectionError::Database)?;
    if capacity == 0 {
        return Ok(ReplaySchedule {
            postponed: true,
            ..ReplaySchedule::default()
        });
    }

    let (repaired, mut scan_incomplete) = repair::repair_jobs(txn, &request, capacity).await?;
    let mut queued = 0;
    if capacity > repaired {
        let (sources, source_incomplete) =
            schedule_sources(txn, &request, capacity - repaired).await?;
        queued = sources;
        scan_incomplete |= source_incomplete;
    }
    Ok(ReplaySchedule {
        queued,
        repaired,
        scan_incomplete,
        postponed: queued + repaired >= capacity,
    })
}

async fn schedule_sources(
    txn: &mut Transaction<'_, Postgres>,
    request: &ScheduleReplay<'_>,
    budget: usize,
) -> Result<(usize, bool), ReplaySelectionError> {
    let target = request.target_parser_version;
    let state = cursor_record(txn, SOURCE_CURSOR_KEY, target).await?;
    let mut phase = Phase::read(&state);
    let mut after = cursor_id(&state);
    let mut selected = 0;
    let mut incomplete = false;
    for _ in 0..2 {
        let page = source_page(txn, target, after, None).await?;
        let next_after = page.next_after;
        for source in page.sources {
            if source.canonical != (phase == Phase::Canonical) {
                continue;
            }
            let dedup = format!("raw-replay:{}:{target}", source.id);
            let payload = json!({
                "raw_ref": source.id.to_string(), "target_version": target,
                "account": request.authenticated_fingerprint, "backfill": true,
            });
            let queued = sqlx::query(
                "INSERT INTO jobs (id, kind, payload, dedup_key, run_at, status, attempts)
                 VALUES ($1, 'raw_replay', $2::jsonb, $3, $4, 'pending', 0)
                 ON CONFLICT (dedup_key) DO NOTHING",
            )
            .bind(Uuid::new_v4())
            .bind(payload.to_string())
            .bind(dedup)
            .bind(OffsetDateTime::from(request.now))
            .execute(&mut **txn)
            .await?;
            if queued.rows_affected() == 0 {
                continue;
            }
            selected += 1;
            if selected == budget {
                after = Some(source.id);
                incomplete = true;
                break;
            }
        }
        if selected == budget {
            break;
        }
        if next_after.is_some() {
            after = next_after;
            incomplete = true;
            break;
        }
        after = None;
        if phase == Phase::Superseded {
            phase = Phase::Canonical;
            incomplete = false;
            break;
        }
        phase = Phase::Superseded;
        incomplete = true;
    }
    write_cursor(txn, SOURCE_CURSOR_KEY, &json!({
        "parser_version": target, "phase": phase.label(), "last_id": after.map(|value| value.to_string()),
    })).await?;
    Ok((selected, incomplete))
}

async fn cursor_record(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    target: i32,
) -> Result<Value, ReplaySelectionError> {
    let saved: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
            .bind(key)
            .fetch_optional(&mut **txn)
            .await?;
    let saved = saved
        .map(|saved| serde_json::from_str::<Value>(&saved))
        .transpose()
        .map_err(|_| ReplaySelectionError::Database)?
        .unwrap_or(Value::Null);
    Ok(
        if saved["parser_version"].as_i64() == Some(i64::from(target)) {
            saved
        } else {
            Value::Null
        },
    )
}

fn cursor_id(saved: &Value) -> Option<Uuid> {
    saved["last_id"]
        .as_str()
        .and_then(|saved| saved.parse().ok())
}

async fn write_cursor(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    value: &Value,
) -> Result<(), ReplaySelectionError> {
    sqlx::query(
        "INSERT INTO app_state (key,value) VALUES ($1,$2::jsonb)
         ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=now()
         WHERE app_state.value IS DISTINCT FROM excluded.value",
    )
    .bind(key)
    .bind(value.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}
