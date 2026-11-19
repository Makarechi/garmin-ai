mod render;

use std::{error::Error, fmt, io::Read, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::JsonDocument;
use ga_domain::{
    legacy_json::{Utf8Layout, encode_ascii},
    timestamp::StoredInstant,
};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{DailyHealthReader, HealthAnalysisError, check_calendar_bounds};
use crate::{
    Database,
    diary::{DiaryReadError, ModelDiaryQuery, model_events_in, owner_events_in},
    scenario_packs::model_disallowed_event_kinds,
    share_policy::{DestinationKind, ShareDestination},
};

const MAX_ANNOTATIONS: u16 = 500;

#[derive(Debug)]
pub enum TimelineError {
    CalendarRange,
    TimestampRange,
    AnnotationLimit,
    Health(HealthAnalysisError),
    Diary(DiaryReadError),
    Database,
}

impl fmt::Display for TimelineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CalendarRange => f.write_str("Date range must be ordered and at most 32 days"),
            Self::TimestampRange => f.write_str("Timestamp range must be ordered and bounded"),
            Self::AnnotationLimit => {
                f.write_str("Timeline exceeds 500 annotations; narrow the interval")
            }
            Self::Health(error) => error.fmt(f),
            Self::Diary(error) => error.fmt(f),
            Self::Database => f.write_str("Timeline storage is unavailable"),
        }
    }
}
impl Error for TimelineError {}

impl From<sqlx::Error> for TimelineError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

#[derive(Serialize)]
struct EventFields {
    topology: String,
    original_start: String,
    missing_end: bool,
}

struct Annotation {
    start: OffsetDateTime,
    end: OffsetDateTime,
    layer: &'static str,
    label: String,
    source: String,
    status: String,
    confidence: f64,
    priority: u8,
    event: Option<EventFields>,
    evidence: Value,
    evidence_order: String,
}

impl Annotation {
    fn clip(&mut self, start: OffsetDateTime, end: OffsetDateTime) {
        // At tied instants, Python keeps the first operand, including its display offset.
        if self.start <= start {
            self.start = start;
        }
        if self.end >= end {
            self.end = end;
        }
    }

    fn from_event(
        event: &Value,
        range_start: OffsetDateTime,
        range_end: OffsetDateTime,
    ) -> Result<Self, TimelineError> {
        let start = text(event, "start")?
            .parse::<StoredInstant>()
            .map_err(|_| TimelineError::Database)?;
        let start = OffsetDateTime::from(start);
        let original_start = text(event, "start")?.to_owned();
        let topology = text(event, "topology")?;
        let missing_end = topology == "open_interval" && event["end"].is_null();
        let end = match event["end"].as_str() {
            Some(value) => value
                .parse::<StoredInstant>()
                .map_err(|_| TimelineError::Database)?
                .into(),
            None if missing_end => range_end,
            None => start,
        };
        let status = text(event, "status")?;
        let kind = text(event, "kind")?;
        let mut annotation = Self {
            start,
            end,
            layer: match kind {
                "nap" => "sleep",
                "migraine"
                | "illness"
                | "medication"
                | "mood"
                | "headache_observation"
                | "wellbeing_observation"
                | "activity_effort" => "wellbeing",
                _ => "context",
            },
            label: event["payload"]["description"]
                .as_str()
                .unwrap_or(kind)
                .to_owned(),
            source: text(event, "source")?.to_owned(),
            status: if status == "confirmed" {
                "known"
            } else {
                status
            }
            .to_owned(),
            confidence: event["confidence"]
                .as_f64()
                .ok_or(TimelineError::Database)?,
            priority: if status == "confirmed" { 2 } else { 0 },
            event: Some(EventFields {
                topology: topology.to_owned(),
                original_start,
                missing_end,
            }),
            evidence: json!({"event_id": text(event, "id")?}),
            evidence_order: String::new(),
        };
        annotation.clip(range_start, range_end);
        Ok(annotation)
    }

    fn missing_end(&self) -> bool {
        self.event.as_ref().is_some_and(|event| event.missing_end)
    }
}

impl Database {
    /// Render the owner's layered timeline from one replay-verified database snapshot.
    pub async fn read_owner_timeline(
        &self,
        start: OffsetDateTime,
        end: OffsetDateTime,
    ) -> Result<Vec<u8>, TimelineError> {
        timeline_range(start, end)?;
        let (annotations, events) = tokio::time::timeout(Duration::from_millis(1500), async {
            self.verified_health_reader()
                .await
                .map_err(TimelineError::Health)?
                .timeline_snapshot(start, end, None)
                .await
        })
        .await
        .map_err(|_| TimelineError::Database)??;
        document_bytes(render_timeline(start, end, annotations, &events)?)
    }
}

impl DailyHealthReader {
    /// Keep the HTTP request's replay fence and display offsets through the timeline read.
    pub async fn owner_timeline(
        self,
        start: OffsetDateTime,
        end: OffsetDateTime,
    ) -> Result<Vec<u8>, TimelineError> {
        timeline_range(start, end)?;
        let (annotations, events) = tokio::time::timeout(
            Duration::from_millis(1500),
            self.timeline_snapshot(start, end, None),
        )
        .await
        .map_err(|_| TimelineError::Database)??;
        document_bytes(render_timeline(start, end, annotations, &events)?)
    }

    /// Stream the owner's layered timeline through the shared bounded spool.
    pub async fn owner_timeline_document(
        self,
        start: OffsetDateTime,
        end: OffsetDateTime,
    ) -> Result<JsonDocument, TimelineError> {
        self.timeline_document(start, end, None).await
    }

    /// Use the exact recorded-version diary policy for the model's timeline.
    pub async fn model_timeline_document(
        self,
        owner: Uuid,
        start: OffsetDateTime,
        end: OffsetDateTime,
    ) -> Result<JsonDocument, TimelineError> {
        self.timeline_document(start, end, Some(owner)).await
    }

    async fn timeline_document(
        self,
        start: OffsetDateTime,
        end: OffsetDateTime,
        owner: Option<Uuid>,
    ) -> Result<JsonDocument, TimelineError> {
        timeline_range(start, end)?;
        let (annotations, events) = tokio::time::timeout(
            Duration::from_millis(1500),
            self.timeline_snapshot(start, end, owner),
        )
        .await
        .map_err(|_| TimelineError::Database)??;
        render_timeline(start, end, annotations, &events)
    }

    async fn timeline_snapshot(
        mut self,
        start: OffsetDateTime,
        end: OffsetDateTime,
        model_owner: Option<Uuid>,
    ) -> Result<(Vec<Annotation>, Value), TimelineError> {
        let mut annotations = read_intervals(&mut self.0, start, end).await?;
        let events = match model_owner {
            Some(owner) => {
                let model = ShareDestination::new(DestinationKind::Model, "model:mcp:local")
                    .map_err(|_| TimelineError::Database)?;
                let page = model_events_in(
                    &mut self.0,
                    ModelDiaryQuery {
                        owner_id: owner,
                        start: StoredInstant::try_from(start)
                            .map_err(|_| TimelineError::TimestampRange)?,
                        end: StoredInstant::try_from(end)
                            .map_err(|_| TimelineError::TimestampRange)?,
                        kind: None,
                        limit: MAX_ANNOTATIONS,
                        model,
                        channel: None,
                    },
                    Some((start, end)),
                )
                .await
                .map_err(TimelineError::Diary)?;
                let denied = model_disallowed_event_kinds(&mut self.0)
                    .await
                    .map_err(|_| TimelineError::Database)?;
                // Python bounds the preliminary SQL candidates before checking grant time.
                // A future-dated facts grant can therefore still exceed this candidate cap.
                let overflow: bool =
                    sqlx::query_scalar(include_str!("timeline/model_candidates.sql"))
                        .bind(start)
                        .bind(end)
                        .bind(denied)
                        .bind(model.instance())
                        .fetch_one(&mut *self.0)
                        .await?;
                if overflow {
                    return Err(TimelineError::AnnotationLimit);
                }
                page.value
            }
            None => owner_events_in(&mut self.0, start, end, None, MAX_ANNOTATIONS)
                .await
                .map_err(TimelineError::Diary)?,
        };
        let entries = events["rows"].as_array().ok_or(TimelineError::Database)?;
        if events["truncated"] == true
            || annotations.len() + entries.len() > usize::from(MAX_ANNOTATIONS)
        {
            return Err(TimelineError::AnnotationLimit);
        }
        for entry in entries {
            annotations.push(Annotation::from_event(entry, start, end)?);
        }
        self.0.commit().await?;
        Ok((annotations, events))
    }
}

fn timeline_range(start: OffsetDateTime, end: OffsetDateTime) -> Result<(), TimelineError> {
    let utc_start: OffsetDateTime = StoredInstant::try_from(start)
        .map_err(|_| TimelineError::TimestampRange)?
        .into();
    let utc_end: OffsetDateTime = StoredInstant::try_from(end)
        .map_err(|_| TimelineError::TimestampRange)?
        .into();
    check_calendar_bounds(utc_start.date(), utc_end.date()).map_err(TimelineError::Health)?;
    if !(0..=32).contains(&(utc_end.date() - utc_start.date()).whole_days()) {
        return Err(TimelineError::CalendarRange);
    }
    if start >= end || end - start > time::Duration::days(31) {
        return Err(TimelineError::TimestampRange);
    }
    Ok(())
}

fn render_timeline(
    start: OffsetDateTime,
    end: OffsetDateTime,
    mut annotations: Vec<Annotation>,
    events: &Value,
) -> Result<JsonDocument, TimelineError> {
    for annotation in &mut annotations {
        let evidence =
            serde_json::to_vec(&annotation.evidence).map_err(|_| TimelineError::Database)?;
        annotation.evidence_order = encode_ascii(&evidence, Utf8Layout::Archive, usize::MAX)
            .map_err(|_| TimelineError::Database)?;
    }
    annotations.sort_by(|left, right| {
        (
            left.layer,
            left.start,
            left.end,
            &left.source,
            &left.evidence_order,
        )
            .cmp(&(
                right.layer,
                right.start,
                right.end,
                &right.source,
                &right.evidence_order,
            ))
    });
    render::timeline(start, end, &annotations, events)
}

async fn read_intervals(
    txn: &mut Transaction<'_, Postgres>,
    start: OffsetDateTime,
    end: OffsetDateTime,
) -> Result<Vec<Annotation>, TimelineError> {
    let activities: Vec<(String, String, OffsetDateTime, OffsetDateTime)> = sqlx::query_as(
        r#"SELECT id,kind,start,"end" FROM activities WHERE start<$2 AND "end">$1 LIMIT 501"#,
    )
    .bind(start)
    .bind(end)
    .fetch_all(&mut **txn)
    .await?;
    if activities.len() > usize::from(MAX_ANNOTATIONS) {
        return Err(TimelineError::AnnotationLimit);
    }
    let mut rows = Vec::with_capacity(activities.len());
    for (id, kind, begin, finish) in activities {
        rows.push(Annotation {
            start: begin,
            end: finish,
            layer: "activity",
            label: kind,
            confidence: 1.0,
            status: "known".into(),
            source: "garmin_activity".into(),
            evidence: json!({"activity_id": id}),
            priority: 3,
            event: None,
            evidence_order: String::new(),
        });
    }
    type IntervalRow = (
        String,
        String,
        f64,
        bool,
        OffsetDateTime,
        OffsetDateTime,
        String,
    );
    let mut intervals = sqlx::query_as::<_, IntervalRow>(
        r#"SELECT label, source, confidence, confirmed, start, "end",
                  evidence::text
           FROM timeline_intervals WHERE start<$2 AND "end">$1 LIMIT $3"#,
    )
    .bind(start)
    .bind(end)
    .bind(i64::from(MAX_ANNOTATIONS) - rows.len() as i64 + 1)
    .fetch(&mut **txn);
    while let Some((label, source, confidence, confirmed, begin, finish, evidence)) =
        intervals.try_next().await?
    {
        if rows.len() == usize::from(MAX_ANNOTATIONS) {
            return Err(TimelineError::AnnotationLimit);
        }
        let planned = matches!(source.as_str(), "calendar" | "external_calendar");
        let layer = if planned {
            "plans"
        } else if matches!(label.as_str(), "sleep" | "nap") {
            "sleep"
        } else {
            "context"
        };
        rows.push(Annotation {
            start: begin,
            end: finish,
            layer,
            label,
            confidence,
            status: if planned {
                "planned"
            } else if confirmed {
                "known"
            } else {
                "inferred"
            }
            .into(),
            priority: if confirmed && source != "garmin_connect" {
                2
            } else {
                1
            },
            source,
            evidence: serde_json::from_str(&evidence).map_err(|_| TimelineError::Database)?,
            event: None,
            evidence_order: String::new(),
        });
    }
    for annotation in &mut rows {
        annotation.clip(start, end);
    }
    Ok(rows)
}

fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str, TimelineError> {
    value[key].as_str().ok_or(TimelineError::Database)
}

fn document_bytes(document: JsonDocument) -> Result<Vec<u8>, TimelineError> {
    let mut bytes = Vec::new();
    document
        .reader()
        .read_to_end(&mut bytes)
        .map_err(|_| TimelineError::Database)?;
    Ok(bytes)
}
