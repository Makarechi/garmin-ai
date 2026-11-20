use std::{error::Error, fmt, io};

use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{DiaryWriteError, metrics::expected_metrics};
use crate::{Database, check_readiness};

#[derive(Debug)]
pub enum ProjectionAuditError {
    Limit,
    Storage,
    Contract(DiaryWriteError),
    Output(io::Error),
}

impl fmt::Display for ProjectionAuditError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Limit => formatter.write_str("Projection audit limit must be between 1 and 1000"),
            Self::Storage => formatter.write_str("Projection audit storage is unavailable"),
            Self::Contract(error) => error.fmt(formatter),
            Self::Output(_) => formatter.write_str("Could not write the projection audit"),
        }
    }
}

impl Error for ProjectionAuditError {}

impl From<sqlx::Error> for ProjectionAuditError {
    fn from(_: sqlx::Error) -> Self {
        Self::Storage
    }
}

impl From<io::Error> for ProjectionAuditError {
    fn from(error: io::Error) -> Self {
        Self::Output(error)
    }
}

#[derive(Default, Serialize)]
pub struct ProjectionTotals {
    events: i64,
    expected: i64,
    valid: i64,
    missing: i64,
    stale: i64,
    mismatched: i64,
    history_unknown: i64,
    pending: i64,
}

impl ProjectionTotals {
    fn add(&mut self, other: &Self) {
        self.events += other.events;
        self.expected += other.expected;
        self.valid += other.valid;
        self.missing += other.missing;
        self.stale += other.stale;
        self.mismatched += other.mismatched;
        self.history_unknown += other.history_unknown;
        self.pending += other.pending;
    }
}

#[derive(Serialize)]
pub struct ProjectionAuditRow {
    event_id: String,
    expected: i64,
    valid: i64,
    missing: i64,
    stale: i64,
    mismatched: i64,
    history_unknown: bool,
    pending: bool,
}

#[derive(Serialize)]
pub struct ProjectionAuditPage {
    #[serde(rename = "type")]
    kind: &'static str,
    page: usize,
    totals: ProjectionTotals,
    rows: Vec<ProjectionAuditRow>,
    writes: bool,
}

impl Database {
    /// Stream custom projection drift under one consistent, read-only snapshot.
    pub async fn audit_custom_projections(
        &self,
        limit: i64,
        mut page_output: impl FnMut(&ProjectionAuditPage) -> io::Result<()>,
    ) -> Result<ProjectionTotals, ProjectionAuditError> {
        let page_size = usize::try_from(limit)
            .ok()
            .filter(|limit| (1..=1000).contains(limit))
            .ok_or(ProjectionAuditError::Limit)?;
        let mut txn = self
            .work
            .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await?;
        sqlx::query("SET LOCAL statement_timeout = '5s'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(|_| ProjectionAuditError::Storage)?;
        let bound: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *txn)
            .await?;
        let (mut since, mut last) = (None::<OffsetDateTime>, None::<Uuid>);
        let mut page_number = 1;
        let mut totals = ProjectionTotals::default();
        loop {
            let events = sqlx::query_as::<_, (Uuid, OffsetDateTime)>(
                "SELECT event.id, event.ingested_at
                 FROM events event
                 JOIN event_definition_versions version ON version.id = event.definition_version_id
                 JOIN event_definitions definition ON definition.id = version.definition_id
                 WHERE definition.namespace = 'user' AND event.ingested_at <= $1
                   AND ($2::timestamptz IS NULL OR (event.ingested_at,event.id) > ($2,$3))
                 ORDER BY event.ingested_at,event.id LIMIT $4",
            )
            .bind(bound)
            .bind(since)
            .bind(last)
            .bind(limit + 1)
            .fetch_all(&mut *txn)
            .await?;
            let more = events.len() > page_size;
            let mut page = ProjectionAuditPage {
                kind: "page",
                page: page_number,
                totals: ProjectionTotals::default(),
                rows: Vec::with_capacity(events.len().min(page_size)),
                writes: false,
            };
            for (event, at) in events.into_iter().take(page_size) {
                let row = audit_event(&mut txn, event).await?;
                page.totals.add(&ProjectionTotals {
                    events: 1,
                    expected: row.expected,
                    valid: row.valid,
                    missing: row.missing,
                    stale: row.stale,
                    mismatched: row.mismatched,
                    history_unknown: i64::from(row.history_unknown),
                    pending: i64::from(row.pending),
                });
                page.rows.push(row);
                since = Some(at);
                last = Some(event);
            }
            page_output(&page)?;
            totals.add(&page.totals);
            if !more {
                break;
            }
            page_number += 1;
        }
        txn.commit().await?;
        Ok(totals)
    }
}

async fn audit_event(
    txn: &mut Transaction<'_, Postgres>,
    event: Uuid,
) -> Result<ProjectionAuditRow, ProjectionAuditError> {
    let (snapshot, deleted, pending): (String, bool, bool) = sqlx::query_as(
        "SELECT jsonb_build_object(
                    'definition_version_id', definition_version_id,
                    'start', start, 'end', \"end\", 'recorded_at', recorded_at,
                    'timezone', timezone, 'payload', payload)::text,
                deleted, NOT deleted AND status <> 'confirmed'
         FROM events WHERE id = $1",
    )
    .bind(event)
    .fetch_one(&mut **txn)
    .await?;
    let snapshot = serde_json::from_str(&snapshot).map_err(|_| ProjectionAuditError::Storage)?;
    let expected = if deleted {
        None
    } else {
        expected_metrics(txn, &snapshot)
            .await
            .map_err(ProjectionAuditError::Contract)?
    };
    let local_date = expected.as_ref().map(|expected| expected.day);
    let mut projected = Vec::new();
    if let Some(expected) = expected {
        for item in expected.values {
            projected.push(json!({
                "field_id": item.field, "metric_version": item.version.to_string(), "metric": item.key,
                "number": item.number, "label": item.text, "flag": item.boolean,
                "unit": item.unit, "sequence": item.sequence
            }));
        }
    }
    let expected = i64::try_from(projected.len()).map_err(|_| ProjectionAuditError::Storage)?;
    let (valid, missing, stale, mismatched, history_unknown) =
        sqlx::query_as::<_, (i64, i64, i64, i64, bool)>(include_str!("projection_audit.sql"))
            .bind(event)
            .bind(Value::Array(projected).to_string())
            .bind(local_date)
            .fetch_one(&mut **txn)
            .await?;
    Ok(ProjectionAuditRow {
        event_id: event.to_string(),
        expected,
        valid,
        missing,
        stale,
        mismatched,
        history_unknown,
        pending,
    })
}
