use std::time::Duration;

use futures_util::TryStreamExt;
use ga_domain::{
    event_input::{local_midnight, source_calendar_date, source_calendar_wall_time},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{ProactiveControlError, policy::lock_notification_policy};
use crate::{Database, check_readiness};

#[derive(Debug, Default, Eq, PartialEq)]
pub struct GeneratedDiaryQuestions {
    pub migraines: u64,
    pub caffeine: u64,
}

impl Database {
    /// Refresh pending diary questions in the existing half-hour generation window.
    pub async fn generate_diary_questions(
        &self,
        owner: Uuid,
        now: StoredInstant,
        default_timezone: &str,
    ) -> Result<GeneratedDiaryQuestions, ProactiveControlError> {
        tokio::time::timeout(Duration::from_secs(5), async {
            let mut txn = self.begin_diary_question_policy().await?;
            let generated = generate_in(&mut txn, owner, now, default_timezone).await?;
            txn.commit().await?;
            Ok(generated)
        })
        .await
        .map_err(|_| ProactiveControlError::Database)?
    }
    pub(super) async fn begin_diary_question_policy(
        &self,
    ) -> Result<Transaction<'static, Postgres>, ProactiveControlError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout='1500ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(ProactiveControlError::Storage)?;
        lock_notification_policy(&mut txn).await?;
        Ok(txn)
    }
}

async fn generate_in(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    now: StoredInstant,
    default_timezone: &str,
) -> Result<GeneratedDiaryQuestions, ProactiveControlError> {
    let timezone = diary_question_timezone(txn, owner, default_timezone).await?;
    let slot = (now.unix_microseconds() / 1_000_000).div_euclid(1800);
    let skip: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM app_state
            WHERE (key='proactive:enabled' AND value->'enabled'='false'::jsonb)
               OR (key='proactive:generation' AND value->'slot'=to_jsonb($1::bigint)))",
    )
    .bind(slot)
    .fetch_one(&mut **txn)
    .await?;
    if skip {
        return Ok(GeneratedDiaryQuestions::default());
    }
    // Leave the same window eligible for the existing Garmin context generator.
    sqlx::query(
        "INSERT INTO app_state(key,value)
         VALUES('proactive:generation',jsonb_build_object('slot',$1::bigint,'context_complete',false))
         ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=clock_timestamp()",
    )
    .bind(slot)
    .execute(&mut **txn)
    .await?;
    let (migraine_enabled, caffeine_enabled) = diary_question_packs(txn, owner).await?;
    let mut generated = GeneratedDiaryQuestions::default();
    if migraine_enabled {
        generated.migraines = generate_migraines(txn, now.into()).await?;
    }
    if caffeine_enabled {
        generated.caffeine = generate_caffeine(txn, now, &timezone).await?;
    }
    Ok(generated)
}

pub(super) async fn diary_question_timezone(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    fallback: &str,
) -> Result<String, ProactiveControlError> {
    sqlx::query_scalar(
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM app_state WHERE key='preferences:onboarding')
                THEN timezone ELSE $2 END FROM people WHERE id=$1",
    )
    .bind(owner)
    .bind(fallback)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(ProactiveControlError::UnknownOwner)
}

pub(super) async fn diary_question_packs(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<(bool, bool), ProactiveControlError> {
    Ok(sqlx::query_as(
        "SELECT
           count(*)=0 OR coalesce(bool_or(pack_key='migraine' AND tracking_enabled AND reminders_enabled),false),
           count(*)=0 OR coalesce(bool_or(pack_key='caffeine' AND tracking_enabled AND reminders_enabled),false)
         FROM module_configs WHERE owner_id=$1",
    )
    .bind(owner)
    .fetch_one(&mut **txn)
    .await?)
}

pub(super) type MigraineQuestionDetails = (
    Uuid,
    OffsetDateTime,
    Option<OffsetDateTime>,
    String,
    bool,
    bool,
);

pub(super) const MIGRAINE_DETAILS: &str = "SELECT episode.id,episode.start,episode.\"end\",episode.timezone,
           EXISTS (SELECT 1 FROM events medication WHERE NOT medication.deleted
               AND medication.kind='medication' AND medication.status='confirmed'
               AND medication.start<=$1 AND medication.payload->>'reason_event_id'=episode.id::text),
           (episode.payload->>'severity' IS NOT NULL OR EXISTS (
               SELECT 1 FROM events symptom WHERE NOT symptom.deleted
                AND symptom.kind='symptom_observation' AND symptom.status='confirmed'
                AND symptom.start<=$1 AND symptom.payload->>'episode_id'=episode.id::text
                AND symptom.payload->>'severity' IS NOT NULL))
         FROM events episode WHERE NOT episode.deleted AND episode.kind='migraine'
           AND episode.status='confirmed'
           AND (($2::uuid IS NOT NULL AND episode.id=$2) OR (
               $2 IS NULL AND (episode.\"end\" IS NULL OR episode.\"end\">$1)
               AND episode.start BETWEEN $1 - interval '48 hours' AND $1 - interval '2 hours'
               AND NOT EXISTS (SELECT 1 FROM pending_questions q WHERE q.dedup_key='migraine:' || episode.id::text)
           ))";

async fn generate_migraines(
    txn: &mut Transaction<'_, Postgres>,
    now: OffsetDateTime,
) -> Result<u64, ProactiveControlError> {
    let rows = sqlx::query_as::<_, MigraineQuestionDetails>(MIGRAINE_DETAILS)
        .bind(now)
        .bind(None::<Uuid>)
        .fetch_all(&mut **txn)
        .await?;
    let mut inserted = 0;
    for episode in rows {
        let event = episode.0;
        inserted += Question {
            kind: "migraine",
            text: migraine_question_text(&episode)?,
            evidence: json!({"event_id": event.to_string()}),
            priority: 0.95,
            event: Some(event),
            key: format!("migraine:{event}"),
        }
        .insert(txn, now)
        .await?;
    }
    Ok(inserted)
}

pub(super) fn migraine_question_text(
    details: &MigraineQuestionDetails,
) -> Result<String, ProactiveControlError> {
    let (_, start, _, zone, has_medication, has_severity) = details;
    let invalid = || ProactiveControlError::InvalidStoredState;
    let start = StoredInstant::try_from(*start).map_err(|_| invalid())?;
    let (day, clock) = source_calendar_wall_time(start, zone).ok_or_else(invalid)?;
    let mut text = format!(
        "Мигрень, начавшаяся {:02}.{:02} в {:02}:{:02}, уже закончилась? Если да — примерно во сколько?",
        day.day(),
        u8::from(day.month()),
        clock.hour(),
        clock.minute()
    );
    if !has_medication {
        text.push_str(" Принимали ли что-нибудь?");
    }
    if !has_severity {
        text.push_str(" Можно также указать силу боли от 0 до 10.");
    }
    Ok(text)
}

async fn generate_caffeine(
    txn: &mut Transaction<'_, Postgres>,
    now: StoredInstant,
    timezone: &str,
) -> Result<u64, ProactiveControlError> {
    let invalid = || ProactiveControlError::InvalidStoredState;
    let (day, clock) = source_calendar_wall_time(now, timezone).ok_or_else(invalid)?;
    if clock.hour() < 15 {
        return Ok(0);
    }
    let first = day
        .checked_sub(time::Duration::days(13))
        .ok_or_else(invalid)?;
    let first = local_midnight(first, timezone).ok_or_else(invalid)?;
    let midnight = local_midnight(day, timezone).ok_or_else(invalid)?;
    let now = OffsetDateTime::from(now);
    let mut days = std::collections::BTreeSet::<Date>::new();
    {
        let mut recent = sqlx::query_scalar::<_, OffsetDateTime>(
            "SELECT start FROM events WHERE NOT deleted AND kind='caffeine' AND status='confirmed'
             AND start BETWEEN $1 AND $2",
        )
        .bind(OffsetDateTime::from(first))
        .bind(now)
        .fetch(&mut **txn);
        while let Some(at) = recent.try_next().await? {
            let at = StoredInstant::try_from(at).map_err(|_| invalid())?;
            let at = source_calendar_date(at, timezone).ok_or_else(invalid)?;
            if at == day {
                return Ok(0);
            }
            days.insert(at);
        }
    }
    if days.len() < 7 {
        return Ok(0);
    }
    let recorded: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM pending_questions WHERE kind='caffeine'
             AND status IN ('sent','uncertain') AND sent_at >= $1 - interval '168 hours')
         OR EXISTS (SELECT 1 FROM events WHERE NOT deleted AND kind='caffeine_absence'
             AND status='confirmed' AND start<=$2 AND \"end\">=$1)",
    )
    .bind(now)
    .bind(OffsetDateTime::from(midnight))
    .fetch_one(&mut **txn)
    .await?;
    if recorded {
        return Ok(0);
    }
    Question {
        kind: "caffeine",
        text: format!("{:02}.{:02}.{:04} кофе был? Если да — примерно когда и сколько?",
            day.day(), u8::from(day.month()), day.year()),
        evidence: json!({"logged_days_last_14": days.len(), "day": day.to_string(), "timezone": timezone}),
        priority: 0.6,
        event: None,
        key: format!("caffeine:{day}"),
    }.insert(txn, now).await
}

struct Question {
    kind: &'static str,
    text: String,
    evidence: Value,
    priority: f64,
    event: Option<Uuid>,
    key: String,
}

impl Question {
    async fn insert(
        self,
        txn: &mut Transaction<'_, Postgres>,
        now: OffsetDateTime,
    ) -> Result<u64, ProactiveControlError> {
        let result = sqlx::query(
            "INSERT INTO pending_questions(id,kind,text,evidence,priority,dedup_key,event_id,
                earliest_send_at,expires_at,attempts,status)
             VALUES(gen_random_uuid(),$1,$2,$3::jsonb,$4,$5,$6,$7,$7+interval '48 hours',0,'pending')
             ON CONFLICT (dedup_key) DO NOTHING",
        )
        .bind(self.kind)
        .bind(self.text)
        .bind(self.evidence.to_string())
        .bind(self.priority)
        .bind(self.key)
        .bind(self.event)
        .bind(now)
        .execute(&mut **txn)
        .await?;
        Ok(result.rows_affected())
    }
}
