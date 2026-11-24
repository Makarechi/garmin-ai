use std::{collections::BTreeMap, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde::Serialize;
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use crate::{Database, check_readiness};

const JOB_KINDS: &[&str] = &[
    "garmin_endpoint",
    "garmin_activities",
    "garmin_fit",
    "raw_replay",
    "source_plugin_poll",
    "channel_initiatives",
    "telegram_update",
    "telegram_control",
    "telegram_ack",
    "telegram_failure",
    "telegram_debug_notice",
    "telegram_connection_notice",
    "telegram_provider_notice",
    "agent_proactive",
    "agent_insights",
    "backup",
    "storage_check",
    "telegram_storage_notice",
];
const JOB_STATUSES: &[&str] = &["pending", "running", "done", "failed"];
const SOURCE_ENDPOINTS: &[&str] = &[
    "daily",
    "steps",
    "heart_rate",
    "sleep",
    "hrv",
    "stress",
    "body_battery",
    "body_battery_events",
    "respiration",
    "spo2",
    "readiness",
    "training_status",
    "max_metrics",
    "endurance",
    "hill",
    "hydration",
    "body_composition",
    "intensity",
    "resting_hr",
    "all_day_events",
    "devices",
    "activity",
    "activity_details",
    "activity_splits",
    "activity_typed_splits",
    "activity_zones",
    "activity_weather",
    "activities",
    "activity_fit",
    "source-record-v1",
];
const SOURCE_STATUSES: &[&str] = &[
    "pending",
    "normalized",
    "archived",
    "empty",
    "error",
    "stale",
    "partial",
    "unsupported",
    "quarantined",
    "raw_only",
];
const CONNECTION_STATES: &[&str] = &[
    "active",
    "degraded",
    "rate_limited",
    "reauth_required",
    "unknown",
];

#[derive(Serialize)]
pub struct OperationsSnapshot {
    runtime_heartbeat_age_seconds: Option<f64>,
    backup_age_seconds: Option<f64>,
    garmin_connection: GarminConnection,
    backup_capacity: Capacity,
    queue_due: Vec<DueQueue>,
    jobs: Vec<JobCount>,
    sources: Vec<SourceCount>,
}

#[derive(Serialize)]
struct GarminConnection {
    state: &'static str,
    paused: bool,
}

#[derive(Default, Serialize)]
struct Capacity {
    available: bool,
    age_seconds: Option<f64>,
    sufficient: Option<bool>,
    volumes: Vec<Volume>,
}

#[derive(Serialize)]
struct Volume {
    role: &'static str,
    free_bytes: i64,
    required_bytes: i64,
}

#[derive(Serialize)]
struct DueQueue {
    lane: &'static str,
    count: i64,
    oldest_due_age_seconds: f64,
}

#[derive(Serialize)]
struct JobCount {
    kind: String,
    status: String,
    count: i64,
}

#[derive(Serialize)]
struct SourceCount {
    endpoint: String,
    status: String,
    count: i64,
}

impl Database {
    /// Aggregate public operational labels from a single database snapshot.
    pub async fn operations_snapshot(&self) -> Option<OperationsSnapshot> {
        tokio::time::timeout(Duration::from_secs(3), async {
            let mut txn = self
                .control
                .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                .await
                .ok()?;
            sqlx::query("SET LOCAL statement_timeout='1500ms'")
                .execute(&mut *txn)
                .await
                .ok()?;
            check_readiness(&mut txn).await.ok()?;
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await
                .ok()?;
            let now = StoredInstant::try_from(at).ok()?;
            let (jobs, queue_due) = jobs(&mut txn, now).await.ok()?;
            let sources = sqlx::query(
                "SELECT CASE WHEN endpoint=ANY($1) THEN endpoint ELSE 'other' END AS label,
                        CASE WHEN status=ANY($2) THEN status ELSE 'other' END AS state,
                        count(*) FROM source_payloads GROUP BY label,state ORDER BY label,state",
            )
            .bind(SOURCE_ENDPOINTS)
            .bind(SOURCE_STATUSES)
            .try_map(|row: sqlx::postgres::PgRow| {
                use sqlx::Row;
                Ok(SourceCount {
                    endpoint: row.try_get(0)?,
                    status: row.try_get(1)?,
                    count: row.try_get(2)?,
                })
            })
            .fetch_all(&mut *txn)
            .await
            .ok()?;
            let values: Option<String> =
                sqlx::query_scalar(include_str!("observability/state.sql"))
                    .fetch_one(&mut *txn)
                    .await
                    .ok()?;
            let values = values
                .as_deref()
                .and_then(|raw| serde_json::from_str(raw).ok())
                .unwrap_or(Value::Null);
            txn.commit().await.ok()?;
            Some(OperationsSnapshot {
                runtime_heartbeat_age_seconds: age(now, &values["runtime:heartbeat"]["at"]),
                backup_age_seconds: age(now, &values["backup:last_success"]["at"]),
                garmin_connection: connection(now, &values["integration:garmin"]),
                backup_capacity: capacity(now, &values["storage:backup-capacity"])
                    .unwrap_or_default(),
                queue_due,
                jobs,
                sources,
            })
        })
        .await
        .ok()
        .flatten()
    }
}

type JobGroup = (String, String, i64, i64, Option<OffsetDateTime>);

async fn jobs(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
) -> Result<(Vec<JobCount>, Vec<DueQueue>), sqlx::Error> {
    let current = OffsetDateTime::from(now);
    let groups: Vec<JobGroup> = sqlx::query_as(include_str!("observability/jobs.sql"))
        .bind(JOB_KINDS)
        .bind(JOB_STATUSES)
        .bind(current)
        .fetch_all(&mut **txn)
        .await?;
    let mut jobs = Vec::with_capacity(groups.len());
    let mut lanes = BTreeMap::new();
    for (kind, status, count, due, oldest) in groups {
        if let Some(oldest) = oldest {
            let lane = match kind.as_str() {
                "garmin_endpoint" | "garmin_activities" | "garmin_fit" | "raw_replay" => "garmin",
                "source_plugin_poll" => "source",
                "channel_initiatives" => "channel",
                "telegram_update"
                | "telegram_control"
                | "telegram_ack"
                | "telegram_failure"
                | "telegram_debug_notice"
                | "telegram_connection_notice"
                | "telegram_provider_notice" => "telegram",
                "agent_proactive" | "agent_insights" => "analysis",
                "backup" => "backup",
                _ => "other",
            };
            let queue = lanes.entry(lane).or_insert(DueQueue {
                lane,
                count: 0,
                oldest_due_age_seconds: 0.0,
            });
            queue.count += due;
            queue.oldest_due_age_seconds = queue
                .oldest_due_age_seconds
                .max((current - oldest).as_seconds_f64());
        }
        jobs.push(JobCount {
            kind,
            status,
            count,
        });
    }
    Ok((jobs, lanes.into_values().collect()))
}

fn timestamp(value: &Value) -> Option<StoredInstant> {
    StoredInstant::from_legacy_input(&Value::String(value.as_str()?.to_owned()))
}

fn age(now: StoredInstant, value: &Value) -> Option<f64> {
    let then = timestamp(value)?;
    Some((OffsetDateTime::from(now) - OffsetDateTime::from(then)).as_seconds_f64())
}

fn connection(now: StoredInstant, value: &Value) -> GarminConnection {
    let state = value["status"].as_str().unwrap_or_default();
    let deadline = &value["blocked_until"];
    let parsed = timestamp(deadline);
    let valid = deadline.is_null() || deadline == "" || deadline == false || parsed.is_some();
    let label = CONNECTION_STATES
        .iter()
        .copied()
        .find(|label| *label == state && valid)
        .unwrap_or("unknown");
    GarminConnection {
        state: label,
        paused: state == "reauth_required" || parsed.is_some_and(|deadline| deadline > now),
    }
}

fn capacity(now: StoredInstant, value: &Value) -> Option<Capacity> {
    let age_seconds = age(now, &value["at"]).filter(|age| *age >= 0.0)?;
    let expected = match value["status"].as_str()? {
        "ready" => true,
        "insufficient" => false,
        _ => return None,
    };
    let configured = value["volumes"]
        .as_array()
        .filter(|items| (1..=2).contains(&items.len()))?;
    let volumes: Vec<_> = configured
        .iter()
        .map(|volume| {
            let role = match volume["role"].as_str()? {
                "shared" => "shared",
                "staging" => "staging",
                "destination" => "destination",
                _ => return None,
            };
            Some(Volume {
                role,
                free_bytes: volume["free_bytes"].as_i64().filter(|value| *value >= 0)?,
                required_bytes: volume["required_bytes"]
                    .as_i64()
                    .filter(|value| *value >= 0)?,
            })
        })
        .collect::<Option<_>>()?;
    let mut roles: Vec<_> = volumes.iter().map(|volume| volume.role).collect();
    roles.sort_unstable();
    let sufficient = volumes
        .iter()
        .all(|volume| volume.free_bytes >= volume.required_bytes);
    if !matches!(roles.as_slice(), ["shared"] | ["destination", "staging"])
        || sufficient != expected
    {
        return None;
    }
    Some(Capacity {
        available: true,
        age_seconds: Some(age_seconds),
        sufficient: Some(sufficient),
        volumes,
    })
}

impl OperationsSnapshot {
    #[must_use]
    pub fn prometheus(&self) -> String {
        let mut lines = Vec::new();
        for (name, age) in [
            ("runtime_heartbeat", self.runtime_heartbeat_age_seconds),
            ("backup", self.backup_age_seconds),
        ] {
            lines.push(format!(
                "garmin_ai_{name}_age_seconds {}",
                age.map_or("-1".to_owned(), |value| value.to_string())
            ));
        }
        for row in &self.jobs {
            lines.push(format!(
                "garmin_ai_jobs{{kind=\"{}\",status=\"{}\"}} {}",
                row.kind, row.status, row.count
            ));
        }
        for row in &self.sources {
            lines.push(format!(
                "garmin_ai_sources{{endpoint=\"{}\",status=\"{}\"}} {}",
                row.endpoint, row.status, row.count
            ));
        }
        for row in &self.queue_due {
            lines.push(format!(
                "garmin_ai_queue_due_count{{lane=\"{}\"}} {}",
                row.lane, row.count
            ));
            lines.push(format!(
                "garmin_ai_queue_due_oldest_due_age_seconds{{lane=\"{}\"}} {}",
                row.lane, row.oldest_due_age_seconds
            ));
        }
        let capacity = &self.backup_capacity;
        lines.push(format!(
            "garmin_ai_backup_capacity_available {}",
            u8::from(capacity.available)
        ));
        if let (Some(age), Some(sufficient)) = (capacity.age_seconds, capacity.sufficient) {
            lines.push(format!("garmin_ai_backup_capacity_age_seconds {age}"));
            lines.push(format!(
                "garmin_ai_backup_capacity_sufficient {}",
                u8::from(sufficient)
            ));
            for volume in &capacity.volumes {
                lines.push(format!(
                    "garmin_ai_backup_capacity_free_bytes{{role=\"{}\"}} {}",
                    volume.role, volume.free_bytes
                ));
                lines.push(format!(
                    "garmin_ai_backup_capacity_required_bytes{{role=\"{}\"}} {}",
                    volume.role, volume.required_bytes
                ));
            }
        }
        lines.push(format!(
            "garmin_ai_garmin_paused {}",
            u8::from(self.garmin_connection.paused)
        ));
        for state in CONNECTION_STATES {
            lines.push(format!(
                "garmin_ai_garmin_connection_state{{state=\"{state}\"}} {}",
                u8::from(*state == self.garmin_connection.state)
            ));
        }
        lines.push(String::new());
        lines.join("\n")
    }
}
