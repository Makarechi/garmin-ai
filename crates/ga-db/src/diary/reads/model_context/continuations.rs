use std::collections::{BTreeMap, BTreeSet};

use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

mod legacy_memory;

use super::{
    DiaryReadError, InterpretationEvents, MENTION_LIMIT, ModelAudience, context_event,
    fetch_context, model_diary_allowed, read_window,
};
use crate::{
    Database,
    conversation_memory::{self, ConversationBinding, MemoryFence},
    diary::{ClarificationClock, clarification::pending_in},
    scenario_packs::model_disallowed_event_kinds,
    share_policy::{DestinationKind, ShareDestination},
};

const QUESTION_LIMIT: usize = 64;

pub struct InterpretationContextQuery<'a> {
    pub binding: ConversationBinding,
    pub model: ShareDestination<'a>,
    pub now: StoredInstant,
    pub clarification_clock: ClarificationClock,
    pub mentioned_events: &'a [Uuid],
}

pub struct InterpretationContext {
    pub channel: String,
    pub events: InterpretationEvents,
    pub recent_questions: Vec<Value>,
    pub pending_clarification: Option<Value>,
    pub selection_unavailable: bool,
    pub recent_analysis_question: Option<Value>,
    pub memory_fence: MemoryFence,
}

type Question = (
    Uuid,
    String,
    Option<Uuid>,
    OffsetDateTime,
    OffsetDateTime,
    OffsetDateTime,
    String,
);
type RelatedRecords = BTreeMap<Uuid, (Value, Option<Uuid>)>;

impl Database {
    /// Read the diary context for one owner conversation without updating its retained history.
    pub async fn interpretation_context(
        &self,
        input: InterpretationContextQuery<'_>,
    ) -> Result<InterpretationContext, DiaryReadError> {
        if input.mentioned_events.len() > MENTION_LIMIT {
            return Err(DiaryReadError::Limit);
        }
        if input.model.kind() != DestinationKind::Model {
            return Err(DiaryReadError::InvalidDestination);
        }
        tokio::time::timeout(std::time::Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            model_diary_allowed(&mut txn,input.binding.owner_id).await?;
            let (channel, pending) = pending_in(&mut txn,input.binding,input.clarification_clock)
                .await.map_err(|_| DiaryReadError::ContextChanged)?;
            ShareDestination::new(DestinationKind::Channel,&channel)
                .map_err(|_| DiaryReadError::InvalidDestination)?;
            let audience = ModelAudience {
                model:input.model.instance(), channel:Some(&channel),
                pack_denials:model_disallowed_event_kinds(&mut txn).await.map_err(|_| DiaryReadError::Database)?,
            };
            let packs = model_packs(&mut txn,input.binding.owner_id).await?;
            let now = OffsetDateTime::from(input.now);
            let mut events = fetch_context(&mut txn,&audience,now,input.mentioned_events).await?;
            let mut continuations = Continuations {
                events: &mut events,
                allow_additional_events: input.mentioned_events.is_empty(),
            };
            let recent_questions =
                continuations.questions(&mut txn, &audience, &packs, now).await?;
            let (pending_clarification, selection_unavailable) =
                continuations.clarification(&mut txn, &audience, &packs, now, pending).await?;
            let memory = conversation_memory::snapshot(&mut txn,input.binding,input.now,false)
                .await.map_err(|_| DiaryReadError::ContextChanged)?;
            let native_question = memory.turns.last().and_then(|turn| {
                Some(json!({"question":turn["question"].as_str()?.chars().take(400).collect::<String>(),
                    "asked_at":turn["asked_at"].as_str()?}))
            });
            let legacy_question = legacy_memory::recent_question(&mut txn,&channel,input.now).await?;
            let recent_analysis_question = [native_question,legacy_question].into_iter().flatten()
                .max_by_key(|question| StoredInstant::from_legacy_input(&question["asked_at"]));
            txn.commit().await?;
            Ok(InterpretationContext {
                channel,events,recent_questions,pending_clarification,selection_unavailable,recent_analysis_question,
                memory_fence:memory.fence,
            })
        }).await.map_err(|_| DiaryReadError::Database)?
    }
}

async fn model_packs(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<Option<BTreeSet<String>>, sqlx::Error> {
    let rows: Vec<(String, bool)> =
        sqlx::query_as("SELECT pack_key,llm_enabled FROM module_configs WHERE owner_id=$1")
            .bind(owner)
            .fetch_all(&mut **txn)
            .await?;
    Ok((!rows.is_empty()).then(|| {
        rows.into_iter()
            .filter_map(|(pack, enabled)| enabled.then_some(pack))
            .collect()
    }))
}

fn permits(packs: &Option<BTreeSet<String>>, key: &str) -> bool {
    packs.as_ref().is_none_or(|packs| packs.contains(key))
}

struct Continuations<'a> {
    events: &'a mut InterpretationEvents,
    allow_additional_events: bool,
}

impl Continuations<'_> {
    async fn questions(
        &mut self,
        txn: &mut Transaction<'_, Postgres>,
        audience: &ModelAudience<'_>,
        packs: &Option<BTreeSet<String>>,
        now: OffsetDateTime,
    ) -> Result<Vec<Value>, DiaryReadError> {
        let mut visible = Vec::new();
        let mut cursor: (Option<OffsetDateTime>, Option<Uuid>) = (None, None);
        loop {
            let mut rows: Vec<Question> = sqlx::query_as(
                "SELECT q.id,q.kind,q.event_id,q.sent_at,q.earliest_send_at,q.expires_at,row_to_json(q)::text
                 FROM pending_questions q
                 WHERE q.status IN ('sent','uncertain','acknowledged') AND q.expires_at>$1 AND q.sent_at<=$1
                   AND ($2::timestamptz IS NULL OR (q.sent_at,q.id)<($2,$3))
                 ORDER BY q.sent_at DESC,q.id DESC LIMIT 32",
            ).bind(now).bind(cursor.0).bind(cursor.1).fetch_all(&mut **txn).await?;
            if rows.is_empty() {
                return Ok(visible);
            }
            if let Some(row) = rows.last() {
                cursor = (Some(row.3), Some(row.0));
            }
            rows.retain(|row| match row.1.as_str() {
                "migraine" | "caffeine" => permits(packs, &row.1),
                "context" => permits(packs, "wellbeing") && permits(packs, "general_diary"),
                _ => true,
            });
            let ids: Vec<_> = rows.iter().filter_map(|row| row.2).collect();
            let related = related_events(txn, audience, now, &ids).await?;
            for (_, _, target, sent, earliest, expires, raw) in rows {
                if let Some(target) = target {
                    let Some((row, version)) = related.get(&target) else {
                        continue;
                    };
                    self.add_event(target, row, *version, now);
                }
                let mut question: Value =
                    serde_json::from_str(&raw).map_err(|_| DiaryReadError::InvalidEvent)?;
                for (field, at) in [
                    ("sent_at", sent),
                    ("earliest_send_at", earliest),
                    ("expires_at", expires),
                ] {
                    question[field] = json!(
                        StoredInstant::try_from(at)
                            .map_err(|_| DiaryReadError::InvalidEvent)?
                            .to_string()
                    );
                }
                question["priority"] = json!(
                    question["priority"]
                        .as_f64()
                        .ok_or(DiaryReadError::InvalidEvent)?
                );
                visible.push(question);
                if visible.len() == QUESTION_LIMIT {
                    return Ok(visible);
                }
            }
        }
    }

    async fn clarification(
        &mut self,
        txn: &mut Transaction<'_, Postgres>,
        audience: &ModelAudience<'_>,
        packs: &Option<BTreeSet<String>>,
        now: OffsetDateTime,
        pending: Option<Value>,
    ) -> Result<(Option<Value>, bool), DiaryReadError> {
        let Some(pending) = pending else {
            return Ok((None, false));
        };
        let explicit_target = pending["explicit_selector"] == true
            && matches!(pending["action"].as_str(), Some("update" | "close"));
        let mut ids = Vec::new();
        if let Some(requested) = pending.get("event_ids") {
            let Some(requested) = requested
                .as_array()
                .filter(|ids| ids.len() <= MENTION_LIMIT)
            else {
                return Ok((None, explicit_target));
            };
            for id in requested {
                let Some(id) = id.as_str().and_then(|id| id.parse().ok()) else {
                    return Ok((None, explicit_target));
                };
                ids.push(id);
            }
        }
        if explicit_target && ids.len() != 1 {
            return Ok((None, true));
        }
        let pack = pending["pack"]
            .as_str()
            .or_else(|| match pending["button"].as_str()? {
                "coffee" | "coffee_preset" => Some("caffeine"),
                "migraine" | "end" | "medication" => Some("migraine"),
                "alcohol" | "note" => Some("general_diary"),
                _ => None,
            });
        let related = related_events(txn, audience, now, &ids).await?;
        let available = ids.iter().all(|id| related.contains_key(id))
            && pack.is_none_or(|pack| permits(packs, pack));
        if explicit_target && !available {
            // The model must never receive fields from a withdrawn record or
            // confuse the continuation with an independent diary report.
            return Ok((None, true));
        }
        for id in ids {
            if let Some((row, version)) = related.get(&id) {
                self.add_event(id, row, *version, now);
            }
        }
        Ok((available.then_some(pending), false))
    }

    fn add_event(&mut self, id: Uuid, row: &Value, version: Option<Uuid>, now: OffsetDateTime) {
        self.events.tracker_versions.extend(version);
        self.events.channel_versions.extend(version);
        if self.allow_additional_events
            && StoredInstant::from_legacy_input(&row["start"])
                .is_some_and(|start| OffsetDateTime::from(start) <= now)
            && !self.events.entries.iter().any(|entry| {
                entry["id"].as_str().and_then(|id| id.parse::<Uuid>().ok()) == Some(id)
            })
        {
            self.events.entries.push(row.clone());
        }
    }
}

async fn related_events(
    txn: &mut Transaction<'_, Postgres>,
    audience: &ModelAudience<'_>,
    now: OffsetDateTime,
    ids: &[Uuid],
) -> Result<RelatedRecords, DiaryReadError> {
    if ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let rows = read_window(txn, audience, (now, now), ids.len(), Some(ids), false).await?;
    rows.into_iter()
        .map(|row| {
            let (id, event, version) = context_event(row)?;
            Ok((id, (event, version)))
        })
        .collect()
}
