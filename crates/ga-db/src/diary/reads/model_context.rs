use std::collections::BTreeSet;

use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

use super::{DiaryReadError, EventRow, ModelAudience, event_snapshot, model_diary_allowed};
use crate::{
    Database,
    scenario_packs::model_disallowed_event_kinds,
    share_policy::{DestinationKind, ShareDestination},
};

mod continuations;
pub(crate) use continuations::interpretation_context_in;
pub use continuations::{InterpretationContext, InterpretationContextQuery};

const RECENT_LIMIT: usize = 12;
const OPEN_LIMIT: usize = 20;
const MENTION_LIMIT: usize = 20;
const CONTEXT_EVENTS: &str = select_events!(
    "AND CASE WHEN $8::uuid[] IS NOT NULL THEN event.id=ANY($8)
              WHEN $9 THEN event.start<=$2
                AND event.topology='open_interval' AND (event.\"end\" IS NULL OR event.\"end\">$2)
              ELSE event.start BETWEEN $1 AND $2 END
     ORDER BY array_position($8::uuid[],event.id),event.start DESC,event.id LIMIT $4"
);

pub struct InterpretationEventsQuery<'a> {
    pub owner: Uuid,
    pub model: ShareDestination<'a>,
    pub channel: Option<ShareDestination<'a>>,
    pub now: StoredInstant,
    pub mentioned: &'a [Uuid],
}

pub struct InterpretationEvents {
    pub entries: Vec<Value>,
    pub history_truncated: bool,
    pub tracker_versions: BTreeSet<Uuid>,
    pub channel_versions: BTreeSet<Uuid>,
}

impl Database {
    /// Select the same bounded latest and open records used by diary interpretation.
    /// A missing explicit reference has the same result as an unshared or deleted one.
    pub async fn interpretation_events(
        &self,
        input: InterpretationEventsQuery<'_>,
    ) -> Result<InterpretationEvents, DiaryReadError> {
        if input.mentioned.len() > MENTION_LIMIT {
            return Err(DiaryReadError::Limit);
        }
        if input.model.kind() != DestinationKind::Model
            || input
                .channel
                .is_some_and(|channel| channel.kind() != DestinationKind::Channel)
        {
            return Err(DiaryReadError::InvalidDestination);
        }
        tokio::time::timeout(std::time::Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            model_diary_allowed(&mut txn, input.owner).await?;
            let audience = ModelAudience {
                model: input.model.instance(),
                channel: input.channel.map(ShareDestination::instance),
                pack_denials: model_disallowed_event_kinds(&mut txn)
                    .await
                    .map_err(|_| DiaryReadError::Database)?,
            };
            let result =
                fetch_context(&mut txn, &audience, input.now.into(), input.mentioned).await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }
}

async fn fetch_context(
    txn: &mut Transaction<'_, Postgres>,
    audience: &ModelAudience<'_>,
    now: OffsetDateTime,
    mentioned: &[Uuid],
) -> Result<InterpretationEvents, DiaryReadError> {
    let start = now
        .checked_sub(Duration::days(14))
        .ok_or(DiaryReadError::TimestampRange)?;
    let mut history_truncated = false;
    let mut rows;
    if mentioned.is_empty() {
        rows = read_window(txn, audience, (start, now), RECENT_LIMIT + 1, None, false).await?;
        let mut open = read_window(txn, audience, (start, now), OPEN_LIMIT + 1, None, true).await?;
        history_truncated = rows.len() > RECENT_LIMIT || open.len() > OPEN_LIMIT;
        rows.truncate(RECENT_LIMIT);
        open.truncate(OPEN_LIMIT);
        rows.extend(open);
    } else {
        rows = read_window(
            txn,
            audience,
            (start, now),
            MENTION_LIMIT,
            Some(mentioned),
            false,
        )
        .await?;
        if rows.len() != mentioned.iter().collect::<BTreeSet<_>>().len() {
            return Err(DiaryReadError::ContextChanged);
        }
    }

    let mut ids = BTreeSet::new();
    let mut tracker_versions = BTreeSet::new();
    let mut entries = Vec::new();
    for row in rows {
        let (id, event, version) = context_event(row)?;
        if !ids.insert(id) {
            continue;
        }
        tracker_versions.extend(version);
        entries.push(event);
    }
    let channel_versions = audience
        .channel
        .map_or_else(BTreeSet::new, |_| tracker_versions.clone());
    Ok(InterpretationEvents {
        entries,
        history_truncated,
        tracker_versions,
        channel_versions,
    })
}

fn context_event(
    (document, start, end, created, updated, recorded, ingested, _): EventRow,
) -> Result<(Uuid, Value, Option<Uuid>), DiaryReadError> {
    let mut event = event_snapshot((document, start, end, created, updated, recorded, ingested))
        .map_err(|_| DiaryReadError::InvalidEvent)?;
    let fields = event.as_object_mut().ok_or(DiaryReadError::InvalidEvent)?;
    let uuid = |field: &str| {
        fields[field]
            .as_str()
            .and_then(|id| id.parse::<Uuid>().ok())
            .ok_or(DiaryReadError::InvalidEvent)
    };
    let id = uuid("id")?;
    let version = fields["kind"]
        .as_str()
        .is_some_and(|kind| kind.starts_with("user."))
        .then(|| uuid("definition_version_id"))
        .transpose()?;
    let confidence = fields["confidence"]
        .as_f64()
        .ok_or(DiaryReadError::InvalidEvent)?;
    fields.insert("confidence".into(), json!(confidence));
    fields.remove("original_text");
    Ok((id, event, version))
}

async fn read_window(
    txn: &mut Transaction<'_, Postgres>,
    audience: &ModelAudience<'_>,
    interval: (OffsetDateTime, OffsetDateTime),
    limit: usize,
    ids: Option<&[Uuid]>,
    open_only: bool,
) -> Result<Vec<EventRow>, sqlx::Error> {
    sqlx::query_as(CONTEXT_EVENTS)
        .bind(interval.0)
        .bind(interval.1)
        .bind(Option::<&str>::None)
        .bind(limit as i64)
        .bind(audience.model)
        .bind(audience.channel)
        .bind(&audience.pack_denials)
        .bind(ids)
        .bind(open_only)
        .fetch_all(&mut **txn)
        .await
}
