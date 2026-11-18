use std::{collections::BTreeSet, error::Error, fmt, time::Duration};

use futures_util::TryStreamExt;
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, Month, OffsetDateTime};
use uuid::Uuid;

use super::{EventSnapshotRow, event_snapshot, load_event};
use crate::{
    Database, ReadinessError, check_readiness,
    conversation_memory::{AnalysisReadContext, analysis_channel_in},
    scenario_packs::model_disallowed_event_kinds,
    share_policy::{DestinationKind, ShareDestination},
    system_definitions::known_system_event,
};

#[derive(Clone, Copy, Debug)]
pub enum DiaryReadError {
    Storage(ReadinessError),
    TimestampRange,
    CalendarRange,
    CalendarBoundary,
    UnknownKind,
    Limit,
    Database,
    InvalidEvent,
    ModelConsent,
    InvalidDestination,
    InvalidZone,
    Owner,
    ContextChanged,
}

impl fmt::Display for DiaryReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Diary storage is unavailable",
            Self::TimestampRange => "Timestamp range must be ordered and bounded",
            Self::CalendarRange => "Date range must be ordered and at most 3661 days",
            Self::CalendarBoundary => "Dates must allow bounded analysis-window expansion",
            Self::UnknownKind => "Unknown event kind",
            Self::Limit => "Invalid event limit",
            Self::Database => "Diary entries could not be read",
            Self::InvalidEvent => "Recorded diary entry could not be interpreted",
            Self::ModelConsent => "The diary model category is disabled",
            Self::InvalidDestination => "Invalid diary sharing destination",
            Self::InvalidZone => "Unknown diary display timezone",
            Self::Owner => "Diary entries belong to a different owner",
            Self::ContextChanged => "Analysis context changed during the question",
        })
    }
}

impl Error for DiaryReadError {}

impl From<sqlx::Error> for DiaryReadError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

pub struct ModelDiaryQuery<'a> {
    pub owner_id: Uuid,
    pub start: StoredInstant,
    pub end: StoredInstant,
    pub kind: Option<&'a str>,
    pub limit: u16,
    pub model: ShareDestination<'a>,
    pub channel: Option<ShareDestination<'a>>,
}

pub struct DiaryEventRange<'a> {
    pub start: StoredInstant,
    pub end: StoredInstant,
    pub kind: Option<&'a str>,
    pub limit: u16,
}

pub struct ModelDiaryPage {
    pub value: Value,
    pub tracker_versions: BTreeSet<Uuid>,
    pub channel_versions: BTreeSet<Uuid>,
}

struct ModelAudience<'a> {
    model: &'a str,
    channel: Option<&'a str>,
    pack_denials: Vec<&'static str>,
}

type EventRow = (
    String,
    OffsetDateTime,
    Option<OffsetDateTime>,
    OffsetDateTime,
    OffsetDateTime,
    OffsetDateTime,
    OffsetDateTime,
    bool,
);

macro_rules! select_events {
    ($scope:literal) => {
        concat!(
            include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/diary/reads/events.sql")),
            include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/share_policy/consent.sql")),
            ") IS NOT TRUE)) ELSE NOT (regexp_replace(event.kind, '^system[.]', '') = ANY($7::text[])) END) ",
            $scope,
        )
    };
}

mod export;
mod model_context;
pub use model_context::{
    InterpretationContext, InterpretationContextQuery, InterpretationEvents,
    InterpretationEventsQuery,
};

const RANGE_EVENTS: &str = select_events!(
    "
    AND event.start < $2
    AND (
        event.\"end\" > $1
        OR (event.\"end\" IS NULL AND event.topology = 'open_interval')
        OR ((event.\"end\" IS NULL OR event.\"end\" = event.start)
            AND event.topology IN ('point', 'flexible') AND event.start >= $1)
    )
    ORDER BY (event.start >= $1) DESC,event.start,event.id LIMIT $4"
);

impl Database {
    /// Read one entry with visibility evaluated against its recorded definition version.
    pub async fn owner_event(
        &self,
        owner: Uuid,
        id: Uuid,
    ) -> Result<Option<Value>, DiaryReadError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let visible: bool = sqlx::query_scalar(
                "SELECT EXISTS(
                    SELECT FROM events AS event WHERE event.id=$2 AND NOT event.deleted
                      AND EXISTS(SELECT FROM people WHERE singleton AND id=$1)
                      AND (event.definition_version_id IS NULL OR EXISTS(
                        SELECT FROM event_definition_versions AS version
                        WHERE version.id=event.definition_version_id
                          AND version.allowed_operations ? 'query')))",
            )
            .bind(owner)
            .bind(id)
            .fetch_one(&mut *txn)
            .await?;
            let result = if visible {
                let stored = load_event(&mut txn, id, false)
                    .await
                    .map_err(|_| DiaryReadError::InvalidEvent)?;
                Some(event_document(stored.snapshot)?)
            } else {
                None
            };
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }

    /// Page the owner's diary. This result is intended for the owner read path.
    pub async fn list_owner_events(
        &self,
        start: StoredInstant,
        end: StoredInstant,
        kind: Option<&str>,
        limit: u16,
    ) -> Result<Value, DiaryReadError> {
        let (start, end) = (OffsetDateTime::from(start), OffsetDateTime::from(end));
        check_range(start, end)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let kind = stored_kind(&mut txn, kind).await?;
            if !(1..=1000).contains(&limit) {
                return Err(DiaryReadError::Limit);
            }
            let response = owner_events_in(&mut txn, start, end, kind.as_deref(), limit).await?;
            txn.commit().await?;
            Ok(response)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }

    /// Return only model-visible entries and the version dependencies for future requests.
    pub async fn list_model_events(
        &self,
        query: ModelDiaryQuery<'_>,
    ) -> Result<ModelDiaryPage, DiaryReadError> {
        let window = model_window(&query)?;
        self.list_scoped_events(query, Some(window)).await
    }

    /// The local MCP model uses its own consent destination. The original
    /// tool evaluates its diary grant before checking the date window.
    pub async fn list_mcp_events(
        &self,
        owner_id: Uuid,
        start: StoredInstant,
        end: StoredInstant,
        kind: Option<&str>,
    ) -> Result<ModelDiaryPage, DiaryReadError> {
        let model = ShareDestination::new(DestinationKind::Model, "model:mcp:local")
            .map_err(|_| DiaryReadError::InvalidDestination)?;
        self.list_scoped_events(
            ModelDiaryQuery {
                owner_id,
                start,
                end,
                kind,
                limit: 500,
                model,
                channel: None,
            },
            None,
        )
        .await
    }

    async fn list_scoped_events(
        &self,
        query: ModelDiaryQuery<'_>,
        validated_window: Option<(OffsetDateTime, OffsetDateTime)>,
    ) -> Result<ModelDiaryPage, DiaryReadError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let page = model_events_in(&mut txn, query, validated_window).await?;
            txn.commit().await?;
            Ok(page)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }

    /// Revalidate the question and diary grant before preparing the next model request.
    pub async fn analysis_diary_context_current(&self, context: AnalysisReadContext<'_>) -> bool {
        matches!(
            tokio::time::timeout(Duration::from_millis(1500), async {
                let mut txn = self.begin_diary_read().await?;
                let channel = analysis_channel_in(&mut txn, context)
                    .await
                    .map_err(|_| DiaryReadError::ContextChanged)?;
                let Some(channel) = channel else {
                    return Ok(false);
                };
                ShareDestination::new(DestinationKind::Channel, &channel)
                    .map_err(|_| DiaryReadError::InvalidDestination)?;
                model_diary_allowed(&mut txn, context.binding.owner_id).await?;
                txn.commit().await?;
                Ok::<_, DiaryReadError>(true)
            })
            .await,
            Ok(Ok(true))
        )
    }

    /// Resolve the delivery channel from the current authenticated question in the same snapshot.
    pub async fn list_analysis_events(
        &self,
        context: AnalysisReadContext<'_>,
        model: ShareDestination<'_>,
        range: DiaryEventRange<'_>,
    ) -> Result<ModelDiaryPage, DiaryReadError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let channel = analysis_channel_in(&mut txn, context)
                .await
                .map_err(|_| DiaryReadError::ContextChanged)?
                .ok_or(DiaryReadError::ContextChanged)?;
            let channel = ShareDestination::new(DestinationKind::Channel, &channel)
                .map_err(|_| DiaryReadError::InvalidDestination)?;
            let query = ModelDiaryQuery {
                owner_id: context.binding.owner_id,
                model,
                channel: Some(channel),
                start: range.start,
                end: range.end,
                kind: range.kind,
                limit: range.limit,
            };
            let window = model_window(&query)?;
            let page = model_events_in(&mut txn, query, Some(window)).await?;
            txn.commit().await?;
            Ok(page)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }

    pub(super) async fn begin_diary_read(
        &self,
    ) -> Result<Transaction<'static, Postgres>, DiaryReadError> {
        let mut txn = self
            .work
            .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(DiaryReadError::Storage)?;
        Ok(txn)
    }
}

async fn model_diary_allowed(
    txn: &mut Transaction<'_, Postgres>,
    owner_id: Uuid,
) -> Result<(), DiaryReadError> {
    let allowed: Option<bool> = sqlx::query_scalar(
        "SELECT COALESCE((SELECT COALESCE(value->'model_categories' @> '[\"diary\"]'::jsonb,false)
            FROM app_state WHERE key='preferences:onboarding'), true)
         FROM people WHERE singleton AND id=$1",
    )
    .bind(owner_id)
    .fetch_optional(&mut **txn)
    .await?;
    match allowed {
        Some(true) => Ok(()),
        Some(false) => Err(DiaryReadError::ModelConsent),
        None => Err(DiaryReadError::Owner),
    }
}

fn model_window(
    query: &ModelDiaryQuery<'_>,
) -> Result<(OffsetDateTime, OffsetDateTime), DiaryReadError> {
    if query.model.kind() != DestinationKind::Model
        || query
            .channel
            .is_some_and(|channel| channel.kind() != DestinationKind::Channel)
    {
        return Err(DiaryReadError::InvalidDestination);
    }
    let (start, end) = (
        OffsetDateTime::from(query.start),
        OffsetDateTime::from(query.end),
    );
    check_range(start, end)?;
    Ok((start, end))
}

async fn model_events_in(
    txn: &mut Transaction<'_, Postgres>,
    query: ModelDiaryQuery<'_>,
    validated_window: Option<(OffsetDateTime, OffsetDateTime)>,
) -> Result<ModelDiaryPage, DiaryReadError> {
    model_diary_allowed(txn, query.owner_id).await?;
    let (start, end) = match validated_window {
        Some(window) => window,
        None => model_window(&query)?,
    };
    let audience = ModelAudience {
        model: query.model.instance(),
        channel: query.channel.map(ShareDestination::instance),
        pack_denials: model_disallowed_event_kinds(txn)
            .await
            .map_err(|_| DiaryReadError::Database)?,
    };
    let kind = match query.kind {
        Some(key) if key.starts_with("user.") => Some(key.to_owned()),
        kind => stored_kind(txn, kind).await?,
    };
    if !(1..=1000).contains(&query.limit) {
        return Err(DiaryReadError::Limit);
    }
    read_events(
        txn,
        start,
        end,
        kind.as_deref(),
        query.limit,
        Some(audience),
    )
    .await
}

pub(crate) async fn owner_events_in(
    txn: &mut Transaction<'_, Postgres>,
    start: OffsetDateTime,
    end: OffsetDateTime,
    kind: Option<&str>,
    limit: u16,
) -> Result<Value, DiaryReadError> {
    Ok(read_events(txn, start, end, kind, limit, None).await?.value)
}

fn check_range(start: OffsetDateTime, end: OffsetDateTime) -> Result<(), DiaryReadError> {
    let earliest = Date::from_calendar_date(1, Month::March, 2)
        .map_err(|_| DiaryReadError::CalendarBoundary)?;
    let latest = Date::from_calendar_date(9999, Month::November, 1)
        .map_err(|_| DiaryReadError::CalendarBoundary)?;
    if start.date() < earliest || end.date() > latest {
        return Err(DiaryReadError::CalendarBoundary);
    }
    if !(0..=3661).contains(&(end.date() - start.date()).whole_days()) {
        return Err(DiaryReadError::CalendarRange);
    }
    if start >= end || end - start > time::Duration::days(3660) {
        return Err(DiaryReadError::TimestampRange);
    }
    Ok(())
}

async fn stored_kind(
    txn: &mut Transaction<'_, Postgres>,
    kind: Option<&str>,
) -> Result<Option<String>, DiaryReadError> {
    let Some(kind) = kind else {
        return Ok(None);
    };
    if known_system_event(kind).map_err(|_| DiaryReadError::InvalidEvent)? {
        return Ok(Some(kind.to_owned()));
    }
    let definition: Option<(String, String)> = sqlx::query_as(
        "SELECT namespace,key FROM event_definitions WHERE key=$1 AND status IN ('active','retired')",
    )
    .bind(kind)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((namespace, key)) = definition else {
        return Err(DiaryReadError::UnknownKind);
    };
    let key = if namespace == "system" {
        key.strip_prefix("system.").unwrap_or(&key)
    } else {
        &key
    };
    Ok(Some(key.to_owned()))
}

async fn read_events(
    txn: &mut Transaction<'_, Postgres>,
    start: OffsetDateTime,
    end: OffsetDateTime,
    kind: Option<&str>,
    limit: u16,
    audience: Option<ModelAudience<'_>>,
) -> Result<ModelDiaryPage, DiaryReadError> {
    let mut rows = Vec::new();
    let mut tracker_versions = BTreeSet::new();
    let model = audience.as_ref().map(|audience| audience.model);
    let channel = audience.as_ref().and_then(|audience| audience.channel);
    let denials = audience
        .as_ref()
        .map_or(&[][..], |audience| audience.pack_denials.as_slice());
    let mut query = sqlx::query_as::<_, EventRow>(RANGE_EVENTS)
        .bind(start)
        .bind(end)
        .bind(kind)
        .bind(i64::from(limit) + 1)
        .bind(model)
        .bind(channel)
        .bind(denials)
        .fetch(&mut **txn);
    let mut truncated = false;
    while let Some((document, start, end, created, updated, recorded, ingested, can_update)) =
        query.try_next().await?
    {
        if rows.len() == usize::from(limit) {
            truncated = true;
            break;
        }
        let snapshot: EventSnapshotRow =
            (document, start, end, created, updated, recorded, ingested);
        let mut event = serialize(snapshot, can_update)?;
        if model.is_some() {
            let fields = event.as_object_mut().ok_or(DiaryReadError::InvalidEvent)?;
            fields.remove("original_text");
            if fields["kind"]
                .as_str()
                .is_some_and(|kind| kind.starts_with("user."))
            {
                let version = fields["definition_version_id"]
                    .as_str()
                    .and_then(|id| id.parse::<Uuid>().ok())
                    .ok_or(DiaryReadError::InvalidEvent)?;
                tracker_versions.insert(version);
            }
        }
        rows.push(event);
    }
    let mut response = json!({"truncated": truncated});
    response["rows"] = Value::Array(rows);
    let channel_versions = channel.map_or_else(BTreeSet::new, |_| tracker_versions.clone());
    Ok(ModelDiaryPage {
        value: response,
        tracker_versions,
        channel_versions,
    })
}

fn serialize(row: EventSnapshotRow, can_update: bool) -> Result<Value, DiaryReadError> {
    let snapshot = event_snapshot(row).map_err(|_| DiaryReadError::InvalidEvent)?;
    let mut event = event_document(snapshot)?;
    event["can_update"] = json!(can_update);
    Ok(event)
}

/// Add the API's canonical evidence and status fields to a stored diary record.
pub fn event_document(mut event: Value) -> Result<Value, DiaryReadError> {
    let confidence = event["confidence"]
        .as_f64()
        .ok_or(DiaryReadError::InvalidEvent)?;
    event["confidence"] = json!(confidence);
    event["canonical"] = json!({
        "version": event["envelope_version"],
        "definition_version_id": event["definition_version_id"],
        "observed": {
            "start": event["start"], "end": event["end"], "timezone": event["timezone"],
            "topology": event["topology"], "precision": event["time_precision"],
        },
        "recorded_at": event["recorded_at"],
        "ingested_at": event["ingested_at"],
        "provenance": {
            "assertion_kind": event["assertion_kind"], "producer": event["producer"],
            "transport": event["transport"], "author": event["author"],
            "evidence_refs": event["evidence_refs"], "confidence": confidence,
            "validation_status": event["validation_status"],
        },
    });
    let open = event["topology"] == "open_interval" && event["end"].is_null();
    event["ongoing"] = json!(open);
    event["missing_end"] = json!(open);
    if event["kind"] == "caffeine" {
        event["caffeine_total"] = caffeine_total(&event["payload"])?;
    }
    Ok(event)
}

pub(crate) fn caffeine_total(payload: &Value) -> Result<Value, DiaryReadError> {
    let unknown = json!("unknown");
    let basis = payload.get("dose_basis").unwrap_or(&unknown);
    let dose = |field: &str| -> Result<Option<f64>, DiaryReadError> {
        let Some(value) = payload.get(field).filter(|value| !value.is_null()) else {
            return Ok(None);
        };
        if !matches!(basis.as_str(), Some("total" | "per_serving")) {
            return Ok(None);
        }
        let value = value.as_f64().ok_or(DiaryReadError::InvalidEvent)?;
        let servings = if basis == "per_serving" {
            payload
                .get("servings")
                .map_or(Some(1.0), Value::as_f64)
                .ok_or(DiaryReadError::InvalidEvent)?
        } else {
            1.0
        };
        Ok(Some(value * servings))
    };
    let min = dose("caffeine_mg_min")?;
    let estimate = dose("caffeine_mg_estimate")?;
    let max = dose("caffeine_mg_max")?;
    Ok(json!({
        "min": min,
        "estimate": estimate,
        "max": max,
        "unit": "mg",
        "basis": "total",
        "source_basis": basis,
        "provenance": payload.get("dose_provenance").unwrap_or(&unknown),
        "status": if min.or(estimate).or(max).is_some() { "available" } else { "unknown" },
    }))
}
