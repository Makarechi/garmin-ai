use std::{collections::BTreeSet, error::Error, fmt};

use ga_domain::{event_input::source_calendar_clock, timestamp::StoredInstant};
use serde::Serialize;
use sqlx::{Postgres, Transaction};
use time::{Date, Duration, OffsetDateTime, Weekday};

use crate::{
    Database, ReadinessError,
    activity_scans::{ActivityScanError, StartActivityScans, start_activity_scans_in},
    check_readiness,
    garmin_integration::{GarminIntegrationError, current_source_selected, garmin_pause},
    garmin_owner::{OwnerVerificationError, verify_owner_in_transaction},
    history_windows::{
        DAY_ENDPOINTS, HistorySchedule, HistoryScheduleError, ScheduleHistory, enabled_endpoints,
        schedule_history_in,
    },
    scenario_packs::source_instance_selected,
};

const MUTATION_LOCK: i64 = 72_104_619;
const FREQUENT: &[&str] = &[
    "daily",
    "steps",
    "heart_rate",
    "stress",
    "body_battery",
    "readiness",
];
const MORNING: &[&str] = &["sleep", "hrv", "readiness"];

pub struct ScheduleGarminSync<'a> {
    pub authenticated_fingerprint: &'a str,
    pub source_instance: &'a str,
    pub configured_timezone: &'a str,
    pub history_days: u16,
    pub now: StoredInstant,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GarminSyncSchedule {
    Busy,
    Disabled {
        retired: i64,
    },
    Paused,
    Scheduled {
        history: HistorySchedule,
        activity_pages: usize,
        periodic: u64,
        compacted: u64,
    },
}

#[derive(Debug)]
pub enum GarminSyncScheduleError {
    Storage(ReadinessError),
    Owner(OwnerVerificationError),
    Connection(GarminIntegrationError),
    History(HistoryScheduleError),
    Activity(ActivityScanError),
    InvalidCalendar,
    Database,
}

impl fmt::Display for GarminSyncScheduleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Garmin schedule storage is unavailable",
            Self::Owner(_) => "Garmin schedule owner could not be verified",
            Self::Connection(_) => "Saved Garmin connection state could not be checked",
            Self::History(_) => "Garmin history work could not be scheduled",
            Self::Activity(_) => "Garmin activity work could not be scheduled",
            Self::InvalidCalendar => "Garmin schedule horizon or timezone is invalid",
            Self::Database => "Garmin schedule could not be stored",
        })
    }
}
impl Error for GarminSyncScheduleError {}

impl From<sqlx::Error> for GarminSyncScheduleError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

async fn retire_jobs(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(include_str!("garmin_periodic/retire.sql"))
        .bind(OffsetDateTime::from(now))
        .bind(now.to_string())
        .fetch_one(&mut **txn)
        .await
}

impl Database {
    /// Apply saved onboarding preferences for each current Garmin job.
    pub async fn garmin_job_timezone(
        &self,
        configured_timezone: &str,
    ) -> Result<String, GarminSyncScheduleError> {
        tokio::time::timeout(std::time::Duration::from_millis(1500), async {
            let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(GarminSyncScheduleError::Storage)?;
            let timezone = crate::health_analysis::owner_timezone(&mut txn, configured_timezone)
                .await
                .map_err(|_| GarminSyncScheduleError::Database)?;
            txn.commit().await?;
            Ok(timezone)
        })
        .await
        .map_err(|_| GarminSyncScheduleError::Database)?
    }

    /// Retire existing work when onboarding has disabled this configured source.
    pub async fn retire_deselected_garmin_work(
        &self,
        source_instance: &str,
        now: StoredInstant,
    ) -> Result<i64, GarminSyncScheduleError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout='750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(GarminSyncScheduleError::Storage)?;
        if current_source_selected(&mut txn, source_instance)
            .await
            .map_err(GarminSyncScheduleError::Connection)?
        {
            return Ok(0);
        }
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !locked {
            return Ok(0);
        }
        let retired = retire_jobs(&mut txn, now).await?;
        txn.commit().await?;
        Ok(retired)
    }

    /// Plan one persistent Garmin scheduler cycle using the already verified session identity.
    /// Enqueues work only. The caller supplies an available, configured source instance.
    pub async fn schedule_garmin_sync(
        &self,
        request: ScheduleGarminSync<'_>,
    ) -> Result<GarminSyncSchedule, GarminSyncScheduleError> {
        if request.history_days > 3660 {
            return Err(GarminSyncScheduleError::InvalidCalendar);
        }
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(GarminSyncScheduleError::Storage)?;
        let owner = verify_owner_in_transaction(&mut txn, request.authenticated_fingerprint)
            .await
            .map_err(GarminSyncScheduleError::Owner)?;
        let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !acquired {
            return Ok(GarminSyncSchedule::Busy);
        }
        if !source_instance_selected(&mut txn, request.source_instance).await? {
            let retired = retire_jobs(&mut txn, request.now).await?;
            txn.commit().await?;
            return Ok(GarminSyncSchedule::Disabled { retired });
        }
        if garmin_pause(&mut *txn, request.now)
            .await
            .map_err(GarminSyncScheduleError::Connection)?
            .is_some()
        {
            return Ok(GarminSyncSchedule::Paused);
        }
        let timezone =
            crate::health_analysis::owner_timezone(&mut txn, request.configured_timezone)
                .await
                .map_err(|_| GarminSyncScheduleError::Database)?;
        let timezone = timezone.as_str();
        let (today, hour) = source_calendar_clock(request.now, timezone)
            .ok_or(GarminSyncScheduleError::InvalidCalendar)?;
        let yesterday = today
            .previous_day()
            .ok_or(GarminSyncScheduleError::InvalidCalendar)?;
        let candidates: Vec<_> = DAY_ENDPOINTS
            .iter()
            .copied()
            .chain(["activities", "devices"])
            .collect();
        let endpoints = enabled_endpoints(&mut txn, owner.owner_id(), &candidates)
            .await
            .map_err(GarminSyncScheduleError::History)?;
        let days: Vec<_> = endpoints
            .iter()
            .copied()
            .filter(|name| DAY_ENDPOINTS.contains(name))
            .collect();
        let history = schedule_history_in(
            &mut txn,
            ScheduleHistory {
                authenticated_fingerprint: request.authenticated_fingerprint,
                timezone,
                horizon_days: request.history_days,
                now: request.now,
            },
            yesterday,
            &days,
        )
        .await
        .map_err(GarminSyncScheduleError::History)?;
        let activity_pages = if endpoints.contains(&"activities") {
            start_activity_scans_in(
                &mut txn,
                StartActivityScans {
                    authenticated_fingerprint: request.authenticated_fingerprint,
                    timezone,
                    history_days: request.history_days,
                    now: request.now,
                },
                today,
            )
            .await
            .map_err(GarminSyncScheduleError::Activity)?
        } else {
            0
        };
        let wake = recent_wake_hour(&mut txn, request.now, timezone).await?;
        let plan = periodic_jobs(
            today,
            hour,
            wake,
            request.now,
            &days,
            endpoints.contains(&"devices"),
        )?;
        let (periodic, compacted) = save_periodic_jobs(&mut txn, request.now, &plan).await?;
        sqlx::query(include_str!("garmin_periodic/telegram_refresh.sql"))
            .bind(owner.owner_id())
            .bind(today.to_string())
            .bind(OffsetDateTime::from(request.now))
            .bind(&days)
            .execute(&mut *txn)
            .await?;
        txn.commit().await?;
        Ok(GarminSyncSchedule::Scheduled {
            history,
            activity_pages,
            periodic,
            compacted,
        })
    }
}

async fn recent_wake_hour(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
    timezone: &str,
) -> Result<u8, GarminSyncScheduleError> {
    let ends: Vec<OffsetDateTime> = sqlx::query_scalar(
        r#"SELECT "end" FROM timeline_intervals WHERE label='sleep' AND "end">$1::timestamptz-interval '14 days'
           ORDER BY "end" DESC LIMIT 14"#,
    )
    .bind(OffsetDateTime::from(now))
    .fetch_all(&mut **txn)
    .await?;
    let mut hours = ends
        .into_iter()
        .map(|end| {
            let instant = end.try_into().ok()?;
            source_calendar_clock(instant, timezone).map(|(_, hour)| hour)
        })
        .collect::<Option<Vec<_>>>()
        .ok_or(GarminSyncScheduleError::InvalidCalendar)?;
    hours.sort_unstable();
    Ok(hours.get(hours.len() / 2).copied().unwrap_or(8))
}

#[derive(Serialize)]
struct PeriodicJob<'a> {
    endpoint: &'a str,
    source_key: String,
    dedup: String,
    delay: u32,
    jitter: u8,
}

fn periodic_jobs<'a>(
    today: Date,
    hour: u8,
    wake: u8,
    now: StoredInstant,
    endpoints: &[&'a str],
    devices: bool,
) -> Result<Vec<PeriodicJob<'a>>, GarminSyncScheduleError> {
    let date_key = today.to_string();
    let slot = OffsetDateTime::from(now).unix_timestamp().div_euclid(900);
    let mut jobs = Vec::new();
    let mut seen = BTreeSet::new();
    let mut push = |endpoint, source_key: &str, dedup, delay, jitter| {
        if seen.insert((endpoint, source_key.to_owned())) {
            jobs.push(PeriodicJob {
                endpoint,
                source_key: source_key.to_owned(),
                dedup,
                delay,
                jitter,
            });
        }
    };
    for &endpoint in endpoints.iter().filter(|e| FREQUENT.contains(e)) {
        push(
            endpoint,
            &date_key,
            format!("frequent:{endpoint}:{slot}"),
            0,
            60,
        );
    }
    if matches!((hour + 24 - wake) % 24, 23 | 0..=3) {
        for &endpoint in endpoints.iter().filter(|e| MORNING.contains(e)) {
            push(
                endpoint,
                &date_key,
                format!("morning:{endpoint}:{slot}"),
                0,
                0,
            );
        }
    }
    if (2..5).contains(&hour) {
        let days = if today.weekday() == Weekday::Monday {
            30
        } else {
            7
        };
        for offset in 0..days {
            let day = today
                .checked_sub(Duration::days(i64::from(offset)))
                .ok_or(GarminSyncScheduleError::InvalidCalendar)?
                .to_string();
            for &endpoint in endpoints {
                push(
                    endpoint,
                    &day,
                    format!("reconcile:{date_key}:{endpoint}:{day}"),
                    offset * 30,
                    0,
                );
            }
        }
    }
    if hour >= 18 {
        for &endpoint in endpoints {
            push(
                endpoint,
                &date_key,
                format!("daily:{endpoint}:{date_key}"),
                0,
                120,
            );
        }
    }
    if devices {
        push(
            "devices",
            "global",
            format!("daily:devices:{date_key}"),
            0,
            120,
        );
    }
    Ok(jobs)
}

async fn save_periodic_jobs(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
    plan: &[PeriodicJob<'_>],
) -> Result<(u64, u64), GarminSyncScheduleError> {
    let at = OffsetDateTime::from(now);
    let inserted = sqlx::query(
        "WITH chat_refresh AS MATERIALIZED (
             SELECT payload->>'endpoint' AS endpoint, payload->>'key' AS source_key
             FROM jobs WHERE kind='garmin_endpoint' AND starts_with(dedup_key,'telegram-refresh:')
               AND (status IN ('pending','running') OR
                    (status='done' AND run_at>$1::timestamptz-interval '15 minutes'
                     AND completed_at>$1::timestamptz-interval '3 minutes'))
         )
         INSERT INTO jobs (id,kind,payload,dedup_key,status,run_at,attempts)
         SELECT gen_random_uuid(),'garmin_endpoint',jsonb_build_object('endpoint',endpoint,'key',source_key),
                dedup,'pending',$1::timestamptz+(delay+random()*jitter)*interval '1 second',0
         FROM jsonb_to_recordset($2::jsonb) AS planned(endpoint text,source_key text,dedup text,delay integer,jitter integer)
         WHERE NOT EXISTS (SELECT 1 FROM chat_refresh chat
                           WHERE chat.endpoint=planned.endpoint AND chat.source_key=planned.source_key)
         ON CONFLICT(dedup_key) DO NOTHING",
    )
    .bind(at)
    .bind(serde_json::to_string(plan).map_err(|_| GarminSyncScheduleError::Database)?)
    .execute(&mut **txn).await?.rows_affected();
    let compacted = sqlx::query(include_str!("garmin_periodic/compact.sql"))
        .bind(at)
        .execute(&mut **txn)
        .await?
        .rows_affected();
    Ok((inserted, compacted))
}
