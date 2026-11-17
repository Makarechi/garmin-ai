use std::collections::BTreeSet;

use serde_json::json;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{ScheduleReplay, cursor_id, cursor_record, write_cursor};
use crate::replay::{ReplaySelectionError, SCAN_LIMIT, source_page};

const REPAIR_CURSOR_KEY: &str = "replay:repair_scan_cursor";
const REPAIR_VERSION: i32 = 2;
type JobRow = (
    Uuid,
    Option<String>,
    String,
    Option<i32>,
    Option<i32>,
    Option<i32>,
    bool,
);

pub(super) async fn repair_jobs(
    txn: &mut Transaction<'_, Postgres>,
    request: &ScheduleReplay<'_>,
    budget: usize,
) -> Result<(usize, bool), ReplaySelectionError> {
    let target = request.target_parser_version;
    let state = cursor_record(txn, REPAIR_CURSOR_KEY, target).await?;
    let cursor = cursor_id(&state);
    let mut entries = scan(txn, target, cursor).await?;
    let wrapped = cursor.is_some() && entries.is_empty();
    if wrapped {
        entries = scan(txn, target, None).await?;
    }
    if entries.is_empty() {
        return Ok((0, false));
    }
    let Some(last_scanned) = entries.last().map(|entry| entry.0) else {
        return Ok((0, false));
    };
    let mut incomplete = entries.len() as i64 == SCAN_LIMIT;
    if wrapped && incomplete {
        incomplete = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM jobs
                WHERE kind='raw_replay' AND status IN ('done','failed') AND payload->>'target_version'=$1::int::text
                  AND id > $2)",
        )
        .bind(target).bind(last_scanned).fetch_one(&mut **txn).await?;
    }
    let ids: Vec<Uuid> = entries
        .iter()
        .filter_map(|entry| entry.1.as_deref()?.parse().ok())
        .collect();
    let canonical: BTreeSet<_> = source_page(txn, target, None, Some(&ids))
        .await?
        .sources
        .into_iter()
        .filter(|source| source.canonical)
        .map(|source| source.id)
        .collect();
    let mut repaired = 0;
    let mut last_repaired = last_scanned;
    for (id, raw_ref, status, repair, known_parser, stored_parser, obsolete) in entries {
        let remains = raw_ref
            .as_deref()
            .and_then(|raw| raw.parse::<Uuid>().ok())
            .is_some_and(|raw| canonical.contains(&raw));
        if !(status == "done"
            || repair != Some(REPAIR_VERSION)
            || stored_parser.is_some_and(|parser| known_parser != Some(parser)))
            || !(remains || obsolete)
        {
            continue;
        }
        let metadata =
            json!({"repair_version": REPAIR_VERSION, "repair_parser_version": stored_parser});
        let changed = sqlx::query(
            "WITH available AS (
                 SELECT id FROM jobs WHERE id = $1 AND status IN ('done','failed')
                   FOR UPDATE SKIP LOCKED
             )
             UPDATE jobs SET payload=jobs.payload || $2::jsonb, status='pending', attempts=0,
                   run_at=$3, completed_at=NULL, lease_until=NULL, lease_token=NULL, last_error=NULL
             FROM available WHERE jobs.id=available.id",
        )
        .bind(id)
        .bind(metadata.to_string())
        .bind(OffsetDateTime::from(request.now))
        .execute(&mut **txn)
        .await?;
        if changed.rows_affected() == 1 {
            repaired += 1;
            last_repaired = id;
        }
        if repaired == budget {
            break;
        }
    }
    let examined = if repaired == budget {
        last_repaired
    } else {
        last_scanned
    };
    write_cursor(
        txn,
        REPAIR_CURSOR_KEY,
        &json!({
            "parser_version": target, "last_id": examined.to_string(),
        }),
    )
    .await?;
    Ok((repaired, incomplete))
}

async fn scan(
    txn: &mut Transaction<'_, Postgres>,
    target: i32,
    after: Option<Uuid>,
) -> Result<Vec<JobRow>, ReplaySelectionError> {
    Ok(sqlx::query_as::<_, JobRow>(
        "WITH candidates AS MATERIALIZED (
            SELECT id, status, payload FROM jobs
            WHERE kind='raw_replay' AND status IN ('done','failed')
              AND payload->>'target_version'=$1::int::text AND ($2::uuid IS NULL OR id > $2)
            ORDER BY id LIMIT $3
         )
         SELECT job.id, job.payload->>'raw_ref', job.status, CASE WHEN pg_input_is_valid(job.payload->>'repair_version','integer')
                     THEN (job.payload->>'repair_version')::int END,
                CASE WHEN pg_input_is_valid(job.payload->>'repair_parser_version','integer')
                     THEN (job.payload->>'repair_parser_version')::int END, raw.parser_version,
                coalesce(marker.value->>'status'='obsolete_target', false)
         FROM candidates AS job
         LEFT JOIN source_payloads AS raw ON raw.id = CASE
             WHEN job.payload->>'raw_ref' ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
             THEN (job.payload->>'raw_ref')::uuid END
         LEFT JOIN app_state AS marker ON marker.key = concat('replay:', job.payload->>'raw_ref', ':', $1::int)
         ORDER BY job.id",
    )
    .bind(target).bind(after).bind(SCAN_LIMIT)
    .fetch_all(&mut **txn).await?)
}
