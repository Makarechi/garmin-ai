use std::{collections::BTreeSet, error::Error, fmt, time::Duration};

use futures_util::TryStreamExt;
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, Month, OffsetDateTime};
use uuid::Uuid;

use super::{EventSnapshotRow, event_snapshot};
use crate::{
    Database, ReadinessError, check_readiness,
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
    pub start: StoredInstant,
    pub end: StoredInstant,
    pub kind: Option<&'a str>,
    pub limit: u16,
    pub model: ShareDestination<'a>,
    pub channel: Option<ShareDestination<'a>>,
}

pub struct ModelDiaryPage {
    pub value: Value,
    pub channel_versions: BTreeSet<Uuid>,
}

struct ModelAudience<'a> {
    model: &'a str,
    channel: Option<&'a str>,
    pack_denials: Vec<&'static str>,
}

impl Database {
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

    /// Return only model-visible entries and the version dependencies for a queued channel reply.
    pub async fn list_model_events(
        &self,
        query: ModelDiaryQuery<'_>,
    ) -> Result<ModelDiaryPage, DiaryReadError> {
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
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let diary_allowed: bool = sqlx::query_scalar(
                "SELECT COALESCE((SELECT COALESCE(value->'model_categories' @> '[\"diary\"]'::jsonb,false)
                    FROM app_state WHERE key='preferences:onboarding'), true)",
            )
            .fetch_one(&mut *txn)
            .await?;
            if !diary_allowed {
                return Err(DiaryReadError::ModelConsent);
            }
            let audience = ModelAudience {
                model: query.model.instance(),
                channel: query.channel.map(ShareDestination::instance),
                pack_denials: model_disallowed_event_kinds(&mut txn)
                    .await
                    .map_err(|_| DiaryReadError::Database)?,
            };
            let kind = match query.kind {
                Some(key) if key.starts_with("user.") => Some(key.to_owned()),
                kind => stored_kind(&mut txn, kind).await?,
            };
            if !(1..=1000).contains(&query.limit) {
                return Err(DiaryReadError::Limit);
            }
            let page = read_events(&mut txn, start, end, kind.as_deref(), query.limit, Some(audience)).await?;
            txn.commit().await?;
            Ok(page)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }

    async fn begin_diary_read(&self) -> Result<Transaction<'static, Postgres>, DiaryReadError> {
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
    let mut rows = Vec::new();
    let mut channel_versions = BTreeSet::new();
    let model = audience.as_ref().map(|audience| audience.model);
    let channel = audience.as_ref().and_then(|audience| audience.channel);
    let denials = audience
        .as_ref()
        .map_or(&[][..], |audience| audience.pack_denials.as_slice());
    let mut query = sqlx::query_as::<_, EventRow>(concat!(
        include_str!("reads/events.sql"),
        include_str!("../share_policy/consent.sql"),
        ") IS NOT TRUE)) ELSE NOT (regexp_replace(event.kind, '^system[.]', '') = ANY($7::text[])) END)
         ORDER BY (event.start >= $1) DESC,event.start,event.id LIMIT $4"
    ))
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
            if channel.is_some()
                && fields["kind"]
                    .as_str()
                    .is_some_and(|kind| kind.starts_with("user."))
            {
                let version = fields["definition_version_id"]
                    .as_str()
                    .and_then(|id| id.parse::<Uuid>().ok())
                    .ok_or(DiaryReadError::InvalidEvent)?;
                channel_versions.insert(version);
            }
        }
        rows.push(event);
    }
    let mut response = json!({"truncated": truncated});
    response["rows"] = Value::Array(rows);
    Ok(ModelDiaryPage {
        value: response,
        channel_versions,
    })
}

fn serialize(row: EventSnapshotRow, can_update: bool) -> Result<Value, DiaryReadError> {
    let mut event = event_snapshot(row).map_err(|_| DiaryReadError::InvalidEvent)?;
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
    event["can_update"] = json!(can_update);
    if event["kind"] == "caffeine" {
        event["caffeine_total"] = caffeine_total(&event["payload"])?;
    }
    Ok(event)
}

fn caffeine_total(payload: &Value) -> Result<Value, DiaryReadError> {
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
