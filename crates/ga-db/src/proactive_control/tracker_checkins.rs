use std::time::Duration;

mod dispatch;
mod fallback;
pub(crate) use dispatch::{
    TrackerCheckinDecision, TrackerCheckinFence, revalidate_tracker_checkin,
};
pub(crate) use fallback::remember_tracker_fallback;

use ga_domain::{
    channel::{OutboundIntent, telegram_conversation_id},
    event_input::{local_midnight, local_wall_time, source_calendar_wall_time},
    legacy_json::{Utf8Layout, encode_ascii},
    sha256_hex,
    timestamp::StoredInstant,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction, postgres::PgRow};
use time::{Date, OffsetDateTime, Time, format_description::well_known::Iso8601};
use uuid::Uuid;

use super::{
    NotificationDecision, NotificationExclusions, NotificationPolicy, ProactiveControlError,
    budget::usage_in, policy::decision_in,
};
use crate::{
    Database,
    channel_ingress::telegram_poll::channel_primary,
    outbox_claims::ReplyDestination,
    outbox_enqueue::{QueueIntent, QueueIntentError, queue_outbox_intent},
    share_policy::{DestinationKind, ShareCategory, ShareDestination, ShareRequirement, allows},
    telegram_binding::{TelegramBindingError, confirmed_pairing},
};

const PAGE_SIZE: i64 = 32;
const RULE_PREFIX: &str = "initiative:rule:";
const RULE_NAMESPACE: Uuid = Uuid::from_u128(0x68af2e31_b719_45a1_a934_92ae9c92091b);

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
struct ChannelRef {
    channel: String,
    instance_id: String,
}

struct Conversation {
    id: Uuid,
    channel: String,
    channel_instance_id: String,
}

impl Conversation {
    fn channel_ref(&self) -> ChannelRef {
        ChannelRef {
            channel: self.channel.clone(),
            instance_id: self.channel_instance_id.clone(),
        }
    }

    fn serves(&self, channel: &ChannelRef) -> bool {
        self.channel == channel.channel && self.channel_instance_id == channel.instance_id
    }
}

#[derive(Deserialize)]
struct Routes {
    channel: Option<ChannelRef>,
    #[serde(default)]
    fallback_channels: Vec<ChannelRef>,
}

struct Tracker {
    id: Uuid,
    revision: i32,
    shortcut: Option<String>,
    reminder_enabled: bool,
    reminder_time: Option<String>,
    reminder_timezone: Option<String>,
    key: String,
    status: String,
    definition_id: Uuid,
    version_id: Uuid,
    allows_create: bool,
}

pub struct TrackerReminderRoute<'a> {
    pub owner: Uuid,
    pub telegram_instance: &'a str,
    pub telegram_chat: i64,
}

enum ReminderDestination<'a> {
    Telegram(TrackerReminderRoute<'a>),
    External(ReplyDestination<'a>),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TrackerRulePage {
    pub next: Option<Uuid>,
    pub queued: usize,
}

impl Database {
    /// Refresh and reserve a bounded page of owner-selected tracker check-ins.
    pub async fn queue_tracker_checkins(
        &self,
        route: TrackerReminderRoute<'_>,
        settings: &NotificationPolicy<'_>,
        now: StoredInstant,
        after: Option<Uuid>,
    ) -> Result<TrackerRulePage, ProactiveControlError> {
        self.queue_reminders(ReminderDestination::Telegram(route), settings, now, after)
            .await
    }

    /// Reserve due reminders for an explicitly configured channel with a current owner conversation.
    pub async fn queue_channel_tracker_checkins(
        &self,
        route: ReplyDestination<'_>,
        settings: &NotificationPolicy<'_>,
        now: StoredInstant,
        after: Option<Uuid>,
    ) -> Result<TrackerRulePage, ProactiveControlError> {
        if route.channel == "telegram" {
            return Err(ProactiveControlError::InvalidCommand);
        }
        self.queue_reminders(ReminderDestination::External(route), settings, now, after)
            .await
    }

    async fn queue_reminders(
        &self,
        route: ReminderDestination<'_>,
        settings: &NotificationPolicy<'_>,
        now: StoredInstant,
        after: Option<Uuid>,
    ) -> Result<TrackerRulePage, ProactiveControlError> {
        let limit = settings
            .daily_budget
            .ok_or(ProactiveControlError::InvalidCommand)?;
        if limit > 20 {
            return Err(ProactiveControlError::InvalidCommand);
        }
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_question_policy().await?;
            let page = sync_page(&mut txn, route, settings, limit, now, after).await?;
            txn.commit().await?;
            Ok(page)
        })
        .await
        .map_err(|_| ProactiveControlError::Database)?
    }
}

async fn sync_page(
    txn: &mut Transaction<'_, Postgres>,
    route: ReminderDestination<'_>,
    settings: &NotificationPolicy<'_>,
    limit: u8,
    now: StoredInstant,
    after: Option<Uuid>,
) -> Result<TrackerRulePage, ProactiveControlError> {
    let (owner, channel, instance) = match &route {
        ReminderDestination::Telegram(route) => {
            let owner = confirmed_pairing(txn, route.telegram_instance, route.telegram_chat)
                .await
                .map_err(|error| match error {
                    TelegramBindingError::Missing | TelegramBindingError::Mismatch => {
                        ProactiveControlError::UnknownOwner
                    }
                    TelegramBindingError::Busy => ProactiveControlError::Busy,
                    _ => ProactiveControlError::Database,
                })?;
            if owner != route.owner {
                return Err(ProactiveControlError::UnknownOwner);
            }
            (owner, "telegram", route.telegram_instance)
        }
        ReminderDestination::External(route) => {
            let owner: Option<Uuid> = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
                .fetch_optional(&mut **txn)
                .await?;
            if owner != Some(route.owner_id) {
                return Err(ProactiveControlError::UnknownOwner);
            }
            (route.owner_id, route.channel, route.instance_id)
        }
    };
    if channel == "web"
        && (!channel_primary(txn, channel, instance).await?
            || !sqlx::query_scalar::<_, bool>(
                "SELECT EXISTS(SELECT 1 FROM conversations WHERE id=$1 AND owner_id=$2
                               AND channel='web' AND channel_instance_id='local')",
            )
            .bind(crate::web_chat::conversation_id(owner))
            .bind(owner)
            .fetch_one(&mut **txn)
            .await?)
    {
        return Ok(TrackerRulePage {
            next: None,
            queued: 0,
        });
    }
    let trackers: Vec<Tracker> = sqlx::query(
        "SELECT t.id,t.revision,t.shortcut,t.reminder_enabled,t.reminder_time,t.reminder_timezone,
                d.key,d.status,d.id AS definition_id,v.id AS version_id,
                v.allowed_operations ? 'create' AS allows_create
         FROM tracker_configs t JOIN event_definitions d ON d.id=t.definition_id
              JOIN event_definition_versions v ON v.definition_id=d.id AND v.version=d.current_version
         WHERE t.owner_id=$1 AND ($2::uuid IS NULL OR t.id>$2)
         ORDER BY t.id LIMIT $3",
    )
    .bind(owner)
    .bind(after)
    .bind(PAGE_SIZE)
    .try_map(|row: PgRow| {
        Ok(Tracker {
            id: row.try_get("id")?,
            revision: row.try_get("revision")?,
            shortcut: row.try_get("shortcut")?,
            reminder_enabled: row.try_get("reminder_enabled")?,
            reminder_time: row.try_get("reminder_time")?,
            reminder_timezone: row.try_get("reminder_timezone")?,
            key: row.try_get("key")?,
            status: row.try_get("status")?,
            definition_id: row.try_get("definition_id")?,
            version_id: row.try_get("version_id")?,
            allows_create: row.try_get("allows_create")?,
        })
    })
    .fetch_all(&mut **txn)
    .await?;
    let next = trackers
        .last()
        .filter(|_| trackers.len() == PAGE_SIZE as usize)
        .map(|tracker| tracker.id);
    let onboarding = onboarding_routes(txn).await?;
    if let ReminderDestination::Telegram(route) = &route {
        ensure_telegram_conversation(txn, route).await?;
    }
    fallback::settle_pending(txn, owner, onboarding.as_ref(), now).await?;
    if trackers.is_empty() {
        return Ok(TrackerRulePage { next, queued: 0 });
    }
    let locale: String = sqlx::query_scalar("SELECT locale FROM people WHERE id=$1")
        .bind(owner)
        .fetch_one(&mut **txn)
        .await?;
    let mut queued = 0;
    for tracker in trackers {
        if let Some(rule) = project(
            txn,
            owner,
            &locale,
            settings,
            limit,
            onboarding.as_ref(),
            &tracker,
        )
        .await?
            && rule["primary_channel"]["channel"] == channel
            && rule["primary_channel"]["instance_id"] == instance
        {
            queued += usize::from(queue(txn, owner, settings, &tracker, &rule, now).await?);
        }
    }
    Ok(TrackerRulePage { next, queued })
}

async fn onboarding_routes(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<Option<Routes>, ProactiveControlError> {
    let onboarding: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='preferences:onboarding'")
            .fetch_optional(&mut **txn)
            .await?;
    onboarding
        .as_deref()
        .map(serde_json::from_str)
        .transpose()
        .map_err(|_| ProactiveControlError::InvalidStoredState)
}

async fn ensure_telegram_conversation(
    txn: &mut Transaction<'_, Postgres>,
    route: &TrackerReminderRoute<'_>,
) -> Result<(), sqlx::Error> {
    let id = telegram_conversation_id(route.owner, route.telegram_instance, route.telegram_chat);
    sqlx::query(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,external_conversation_id,
                                   memory_epoch,state,share_owner_memory)
         SELECT $1,$2,'telegram',$3,$4,$5,'{}',false
         WHERE NOT EXISTS(SELECT 1 FROM conversations WHERE owner_id=$2 AND channel='telegram'
                          AND channel_instance_id=$3)
         ON CONFLICT(id) DO NOTHING",
    )
    .bind(id)
    .bind(route.owner)
    .bind(route.telegram_instance)
    .bind(route.telegram_chat.to_string())
    .bind(Uuid::new_v4())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn project(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    locale: &str,
    settings: &NotificationPolicy<'_>,
    limit: u8,
    onboarding: Option<&Routes>,
    tracker: &Tracker,
) -> Result<Option<Value>, ProactiveControlError> {
    let id = tracker_rule_id(tracker.id);
    let key = format!("{RULE_PREFIX}{id}");
    let previous: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(&key)
            .fetch_optional(&mut **txn)
            .await?;
    let previous: Option<Value> = previous
        .as_deref()
        .map(serde_json::from_str)
        .transpose()
        .map_err(|_| ProactiveControlError::InvalidStoredState)?;
    let explicit = onboarding.and_then(|routes| routes.channel.as_ref());
    if !tracker.reminder_enabled
        || tracker.status != "active"
        || tracker.reminder_time.is_none()
        || tracker.reminder_timezone.is_none()
        || (onboarding.is_some() && explicit.is_none())
    {
        if previous.is_some() {
            cancel_queued(txn, id).await?;
        }
        return Ok(None);
    }
    let prior = previous
        .as_ref()
        .and_then(|value| value["conversation_id"].as_str())
        .and_then(|id| id.parse::<Uuid>().ok());
    let conversation: Option<Conversation> = sqlx::query(
        "SELECT id,channel,channel_instance_id FROM conversations
         WHERE owner_id=$1 AND ($2::text IS NULL OR (channel=$2 AND channel_instance_id=$3))
         ORDER BY CASE WHEN $2::text IS NULL AND id=$4 THEN 0 ELSE 1 END,updated_at DESC,id LIMIT 1",
    )
    .bind(owner)
    .bind(explicit.map(|channel| channel.channel.as_str()))
    .bind(explicit.map(|channel| channel.instance_id.as_str()))
    .bind(prior)
    .try_map(|row: PgRow| {
        Ok(Conversation {
            id: row.try_get("id")?,
            channel: row.try_get("channel")?,
            channel_instance_id: row.try_get("channel_instance_id")?,
        })
    })
    .fetch_optional(&mut **txn)
    .await?;
    let Some(conversation) = conversation else {
        if previous.is_some() {
            cancel_queued(txn, id).await?;
        }
        return Ok(None);
    };
    let mut fallbacks = Vec::new();
    if let Some(routes) = onboarding {
        if routes.fallback_channels.len() > 3 {
            return Err(ProactiveControlError::InvalidStoredState);
        }
        for channel in &routes.fallback_channels {
            if !conversation.serves(channel) && unique_route(txn, owner, channel).await? {
                fallbacks.push(channel);
            }
        }
    }

    let label = tracker
        .shortcut
        .as_deref()
        .filter(|value| !value.is_empty())
        .unwrap_or(&tracker.key);
    let prompt = if locale
        .split('-')
        .next()
        .is_some_and(|language| language.eq_ignore_ascii_case("ru"))
    {
        format!("Напоминание: {label}.")
    } else {
        format!("Reminder: {label}.")
    };
    let candidate = json!({
        "id":id.to_string(),"definition_version_id":tracker.version_id.to_string(),"topic":format!("tracker:{}",tracker.id),
        "rule": {
            "kind":"missing_entry","prompt":prompt,
            "local_time":tracker.reminder_time.as_ref().map(|clock| format!("{clock}:00")),
            "threshold_field_id":null,"threshold_operator":null,"threshold_value":null
        },
        "conversation_id":conversation.id.to_string(),"primary_channel":conversation.channel_ref(),
        "fallback_channels":fallbacks,"timezone":tracker.reminder_timezone,
        "enabled":previous.as_ref().and_then(|value| value["enabled"].as_bool()).unwrap_or(true),
        "consented":previous.as_ref().and_then(|value| value["consented"].as_bool()).unwrap_or(true),
        "snoozed_until":previous.as_ref().and_then(|value| value.get("snoozed_until")),
        "quiet_start":wall_clock(settings.quiet_start),"quiet_end":wall_clock(settings.quiet_end),
        "daily_budget":limit,"tracker_revision":tracker.revision,
    });
    let changed = previous.as_ref() != Some(&candidate);
    if changed {
        if let Some(previous) = previous.as_ref()
            && rule_revision(previous.clone())? != rule_revision(candidate.clone())?
        {
            cancel_queued(txn, id).await?;
        }
        sqlx::query(
            "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
             ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
        )
        .bind(key)
        .bind(candidate.to_string())
        .execute(&mut **txn)
        .await?;
        if candidate["enabled"] == false {
            cancel_queued(txn, id).await?;
        }
    }
    Ok(Some(candidate))
}

fn wall_clock(clock: Time) -> String {
    format!(
        "{:02}:{:02}:{:02}",
        clock.hour(),
        clock.minute(),
        clock.second()
    )
}

fn rule_revision(mut rule: Value) -> Result<String, ProactiveControlError> {
    rule.as_object_mut()
        .ok_or(ProactiveControlError::InvalidStoredState)?
        .remove("snoozed_until");
    let encoded = encode_ascii(rule.to_string().as_bytes(), Utf8Layout::Archive, 32 * 1024)
        .map_err(|_| ProactiveControlError::InvalidStoredState)?;
    Ok(sha256_hex(encoded.as_bytes()))
}

async fn unique_route(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    channel: &ChannelRef,
) -> Result<bool, sqlx::Error> {
    Ok(unique_conversation(txn, owner, channel).await?.is_some())
}

async fn unique_conversation(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    channel: &ChannelRef,
) -> Result<Option<Uuid>, sqlx::Error> {
    let candidates = sqlx::query_scalar(
        "SELECT id FROM conversations WHERE owner_id=$1
           AND channel=$2 AND channel_instance_id=$3 LIMIT 2",
    )
    .bind(owner)
    .bind(&channel.channel)
    .bind(&channel.instance_id)
    .fetch_all(&mut **txn)
    .await?;
    Ok(match candidates.as_slice() {
        [id] => Some(*id),
        _ => None,
    })
}

async fn cancel_queued(txn: &mut Transaction<'_, Postgres>, id: Uuid) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE outbox_messages SET state='cancelled',next_attempt_at=NULL,updated_at=now()
         WHERE state='queued' AND intent->'evidence_refs' @> $1::jsonb",
    )
    .bind(json!([format!("rule:{id}")]).to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

struct Occurrence {
    day: Date,
    due: StoredInstant,
    day_start: StoredInstant,
    day_end: StoredInstant,
    carry_until: StoredInstant,
}

impl Occurrence {
    fn at(now: StoredInstant, timezone: &str, clock: Time) -> Option<Self> {
        let (today, time) = source_calendar_wall_time(now, timezone)?;
        let day = if time < clock {
            today.previous_day()?
        } else {
            today
        };
        Self::for_day(day, timezone, clock)
    }

    fn for_day(day: Date, timezone: &str, clock: Time) -> Option<Self> {
        let carry = day
            .with_time(clock)
            .checked_add(time::Duration::hours(12))?;
        Some(Self {
            day,
            due: local_wall_time(day, clock, timezone)?,
            day_start: local_midnight(day, timezone)?,
            day_end: local_midnight(day.next_day()?, timezone)?,
            carry_until: local_wall_time(carry.date(), carry.time(), timezone)?,
        })
    }
}

async fn queue(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    settings: &NotificationPolicy<'_>,
    tracker: &Tracker,
    rule: &Value,
    now: StoredInstant,
) -> Result<bool, ProactiveControlError> {
    let invalid = || ProactiveControlError::InvalidStoredState;
    if rule["enabled"] != true || rule["consented"] != true || !tracker.allows_create {
        return Ok(false);
    }
    let id = tracker_rule_id(tracker.id);
    let marker = format!("rule:{id}");
    let timezone = rule["timezone"].as_str().ok_or_else(invalid)?;
    let clock = Time::parse(
        rule["rule"]["local_time"].as_str().ok_or_else(invalid)?,
        &Iso8601::TIME,
    )
    .map_err(|_| invalid())?;
    let position = Occurrence::at(now, timezone, clock).ok_or_else(invalid)?;
    let target = &rule["primary_channel"];
    let target_id = format!(
        "{}:{}",
        target["channel"].as_str().ok_or_else(invalid)?,
        target["instance_id"].as_str().ok_or_else(invalid)?,
    );
    let destination =
        ShareDestination::new(DestinationKind::Channel, &target_id).map_err(|_| invalid())?;
    let requirement = ShareRequirement {
        version_id: tracker.version_id,
        categories: &[ShareCategory::Schema, ShareCategory::Facts],
    };
    let paused = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='proactive:enabled'
                          AND value->'enabled'='false'::jsonb)",
    )
    .fetch_one(&mut **txn)
    .await?;
    if paused {
        cancel_queued(txn, id).await?;
        return Ok(false);
    }
    if !allows(txn, requirement, destination)
        .await
        .map_err(|_| ProactiveControlError::Database)?
        || has_entry(txn, tracker.definition_id, &position).await?
    {
        return Ok(false);
    }
    let revision = rule_revision(rule.clone())?;
    let day = position.day.to_string();
    let key = format!("{marker}:{revision}:{day}");
    let skip_key = format!("initiative:skip:{id}:{day}");
    let known: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM outbox_messages WHERE dedup_key=$1 OR (
            dedup_key LIKE $2 AND (right(dedup_key,length($3))=$3 OR dedup_key LIKE $4)
            AND state IN ('sending','provider_accepted','delivered','read','uncertain')))
          OR EXISTS(SELECT 1 FROM app_state WHERE key=$5 AND value->>'rule_revision'=$6)",
    )
    .bind(&key)
    .bind(format!("{marker}:%"))
    .bind(format!(":{day}"))
    .bind(format!("%:{day}:fallback:%"))
    .bind(&skip_key)
    .bind(&revision)
    .fetch_one(&mut **txn)
    .await?;
    if known {
        return Ok(false);
    }
    let snoozed = match &rule["snoozed_until"] {
        Value::Null => None,
        value => Some(StoredInstant::from_legacy_input(value).ok_or_else(invalid)?),
    };
    let policy = NotificationPolicy {
        timezone,
        default_enabled: true,
        daily_budget: None,
        snoozed_until: snoozed,
        destination: Some(&target_id),
        exclusions: NotificationExclusions::default(),
        ..*settings
    };
    let (retry, reason) = if now >= position.carry_until {
        (None, Some("service_recovery"))
    } else {
        match decision_in(txn, &policy, now).await? {
            NotificationDecision::Cancel(_) => return Ok(false),
            NotificationDecision::Allow => (None, None),
            NotificationDecision::Defer { reason, retry_at } => (
                Some(retry_at),
                (retry_at >= position.carry_until).then(|| reason.label()),
            ),
        }
    };
    if let Some(reason) = reason {
        skip_occurrence(txn, id, position.day, &revision, reason).await?;
        return Ok(false);
    }
    if snoozed.is_some_and(|until| until > now) {
        return Ok(false);
    }
    let budget_at = retry.unwrap_or(now);
    let limit = settings
        .daily_budget
        .ok_or(ProactiveControlError::InvalidCommand)?;
    if usage_in(txn, budget_at, timezone, policy.exclusions).await? >= i64::from(limit) {
        return Ok(false);
    }
    let expires = if budget_at >= position.day_end {
        position.carry_until
    } else {
        position.day_end
    };
    let intent = OutboundIntent::normalize(json!({
        "owner_id":owner.to_string(),"conversation_id":rule["conversation_id"],
        "channel_instance":target,"blocks":[{"text":rule["rule"]["prompt"]}],
        "evidence_refs":[marker,format!("definition:{}",tracker.definition_id)],
        "initiative":true,"policy_revision":revision,"scheduled_day":day,
        "logical_notification_id": key,"expires_at":expires.to_string(),
    }))
    .map_err(|_| invalid())?;
    let receipt = match queue_outbox_intent(
        txn,
        QueueIntent {
            intent,
            operation_id: Uuid::new_v4(),
            inbound_id: None,
            dedup_key: Some(&key),
            requirements: &[requirement],
            now,
        },
    )
    .await
    {
        Ok(receipt) => receipt,
        Err(QueueIntentError::ConsentRequired) => return Ok(false),
        Err(QueueIntentError::Busy) => return Err(ProactiveControlError::Busy),
        Err(QueueIntentError::Storage(error)) => return Err(ProactiveControlError::Storage(error)),
        Err(_) => return Err(ProactiveControlError::Database),
    };
    if let Some(retry) = retry {
        sqlx::query("UPDATE outbox_messages SET next_attempt_at=$2 WHERE id=$1")
            .bind(receipt.outbox_id)
            .bind(OffsetDateTime::from(retry))
            .execute(&mut **txn)
            .await?;
    }
    Ok(receipt.created)
}

async fn has_entry(
    txn: &mut Transaction<'_, Postgres>,
    definition: Uuid,
    occurrence: &Occurrence,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM events e JOIN event_definition_versions v
             ON v.id=e.definition_version_id WHERE v.definition_id=$1 AND NOT e.deleted
             AND e.start>=$2 AND e.start<$3)",
    )
    .bind(definition)
    .bind(OffsetDateTime::from(occurrence.day_start))
    .bind(OffsetDateTime::from(occurrence.day_end))
    .fetch_one(&mut **txn)
    .await
}

fn tracker_rule_id(tracker: Uuid) -> Uuid {
    Uuid::new_v5(&RULE_NAMESPACE, tracker.to_string().as_bytes())
}

async fn skip_occurrence(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    day: Date,
    revision: &str,
    reason: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
    )
    .bind(format!("initiative:skip:{id}:{day}"))
    .bind(
        json!({"reason":"defer_exceeds_carry_window","policy_reason":reason,
                 "scheduled_day":day.to_string(),"rule_revision":revision})
        .to_string(),
    )
    .execute(&mut **txn)
    .await?;
    Ok(())
}
