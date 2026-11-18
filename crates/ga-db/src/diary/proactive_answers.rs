use ga_domain::{event_input::local_midnight, timestamp::StoredInstant};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, Duration, OffsetDateTime, format_description::well_known::Iso8601};
use uuid::Uuid;

use super::DiaryWriteError;

pub(crate) mod context;

type Row = (
    Uuid,
    String,
    Option<Uuid>,
    String,
    OffsetDateTime,
    OffsetDateTime,
    Option<OffsetDateTime>,
    String,
);

struct Question {
    id: Uuid,
    kind: String,
    event: Option<Uuid>,
    evidence: Map<String, Value>,
    earliest: OffsetDateTime,
    expires: OffsetDateTime,
    sent: Option<OffsetDateTime>,
    status: String,
}

impl Question {
    fn from_row(
        (id, kind, event, evidence, earliest, expires, sent, status): Row,
    ) -> Result<Self, DiaryWriteError> {
        Ok(Self {
            id,
            kind,
            event,
            earliest,
            expires,
            sent,
            status,
            evidence: serde_json::from_str(&evidence)
                .map_err(|_| DiaryWriteError::InvalidRecordedState)?,
        })
    }

    fn reactivate(&mut self, now: OffsetDateTime) {
        self.status = if self.sent.is_some() {
            "sent"
        } else {
            "pending"
        }
        .into();
        for key in [
            "answer_event_id",
            "answer_text",
            "answered_at",
            "acknowledged_events",
        ] {
            self.evidence.remove(key);
        }
        if self.expires <= now {
            self.earliest = now;
            self.expires = now + Duration::days(2);
        }
    }
}

pub(super) async fn reconcile(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    now: OffsetDateTime,
    timezone: &str,
) -> Result<(), DiaryWriteError> {
    let enabled: Vec<(String, bool, bool)> = sqlx::query_as(
        "SELECT pack_key,reminders_enabled,tracking_enabled FROM module_configs WHERE owner_id=$1",
    )
    .bind(owner)
    .fetch_all(&mut **txn)
    .await?;
    let mut last = None;
    loop {
        let page: Vec<Row> = sqlx::query_as(
            "SELECT id,kind,event_id,evidence::text,earliest_send_at,expires_at,sent_at,status
             FROM pending_questions
             WHERE ($2::uuid IS NULL OR id>$2)
               AND (expires_at >= $1 OR (kind='migraine' AND status='cancelled'))
               AND status IN ('pending','sent','uncertain','answered','acknowledged','cancelled')
             ORDER BY id LIMIT 64 FOR UPDATE",
        )
        .bind(now - Duration::days(7))
        .bind(last)
        .fetch_all(&mut **txn)
        .await?;
        if page.is_empty() {
            return Ok(());
        }
        for row in page {
            let mut question = Question::from_row(row)?;
            last = Some(question.id);
            if question.status == "cancelled"
                && question.evidence.get("cancel_reason") == Some(&json!("owner_pause"))
            {
                continue;
            }
            reconcile_question(txn, &mut question, now, timezone, &enabled).await?;
            sqlx::query(
                "UPDATE pending_questions SET status=$2,evidence=$3::jsonb,earliest_send_at=$4,expires_at=$5
                 WHERE id=$1 AND (status,evidence,earliest_send_at,expires_at)
                   IS DISTINCT FROM ($2,$3::jsonb,$4,$5)",
            ).bind(question.id).bind(question.status).bind(Value::Object(question.evidence).to_string())
             .bind(question.earliest).bind(question.expires).execute(&mut **txn).await?;
        }
    }
}

async fn reconcile_question(
    txn: &mut Transaction<'_, Postgres>,
    question: &mut Question,
    now: OffsetDateTime,
    timezone: &str,
    enabled: &[(String, bool, bool)],
) -> Result<(), DiaryWriteError> {
    let permits = |key, tracking: bool| {
        enabled.is_empty()
            || enabled.iter().any(|(pack, reminders, tracked)| {
                pack == key && *reminders && (!tracking || *tracked)
            })
    };
    let allowed = match question.kind.as_str() {
        "migraine" | "caffeine" => permits(question.kind.as_str(), true),
        "context" => permits("wellbeing", false) && permits("general_diary", true),
        _ => true,
    };
    if !allowed {
        question.status = "cancelled".into();
        return Ok(());
    }
    if let Some(events) = question.evidence.get("acknowledged_events")
        && valid_events(txn, events).await? == Some(false)
    {
        question.reactivate(now);
    }
    let answer = match (question.kind.as_str(), question.event) {
        ("migraine", Some(id)) => {
            let observed: Option<Option<OffsetDateTime>> = sqlx::query_scalar(
                "SELECT \"end\" FROM events WHERE id=$1 AND NOT deleted AND kind='migraine'
                 AND status='confirmed' AND start<=$2
                 AND (\"end\" <= $2 OR start BETWEEN $2-interval '48 hours' AND $2-interval '2 hours')",
            ).bind(id).bind(now).fetch_optional(&mut **txn).await?;
            match observed {
                None => {
                    question.status = "cancelled".into();
                    return Ok(());
                }
                Some(end) => end.filter(|end| *end <= now).map(|_| id),
            }
        }
        ("caffeine", _)
            if question
                .evidence
                .get("day")
                .and_then(Value::as_str)
                .is_some_and(|day| !day.is_empty()) =>
        {
            caffeine_answer(txn, question, now).await?
        }
        ("context", _)
            if question
                .evidence
                .get("start")
                .and_then(Value::as_str)
                .is_some_and(|start| !start.is_empty()) =>
        {
            match context::answer(txn, question, now, timezone).await? {
                context::Outcome::Finished => return Ok(()),
                context::Outcome::Answer(answer) => answer,
            }
        }
        _ => return Ok(()),
    };
    if let Some(answer) = answer {
        question.status = "answered".into();
        question
            .evidence
            .insert("answer_event_id".into(), json!(answer.to_string()));
    } else if question.status == "answered"
        || (question.status == "cancelled" && question.kind == "migraine")
    {
        question.reactivate(now);
    }
    Ok(())
}

async fn caffeine_answer(
    txn: &mut Transaction<'_, Postgres>,
    question: &Question,
    now: OffsetDateTime,
) -> Result<Option<Uuid>, DiaryWriteError> {
    let day = Date::parse(
        question.evidence["day"]
            .as_str()
            .ok_or(DiaryWriteError::InvalidRecordedState)?,
        &Iso8601::DATE,
    )
    .map_err(|_| DiaryWriteError::InvalidRecordedState)?;
    let zone = question
        .evidence
        .get("timezone")
        .and_then(Value::as_str)
        .unwrap_or("Europe/Bratislava");
    let left = local_midnight(day, zone).ok_or(DiaryWriteError::InvalidRecordedState)?;
    let right = local_midnight(
        day.next_day()
            .ok_or(DiaryWriteError::InvalidRecordedState)?,
        zone,
    )
    .ok_or(DiaryWriteError::InvalidRecordedState)?;
    let visible_until = now
        .min(right.into())
        .min(question.sent.unwrap_or(question.earliest));
    Ok(sqlx::query_scalar(
        "SELECT id FROM events WHERE NOT deleted AND status='confirmed'
          AND ((kind='caffeine' AND start >= $1 AND start < $2 AND start <= $3)
            OR (kind='caffeine_absence' AND start <= $1 AND \"end\" >= $4))
         ORDER BY start LIMIT 1",
    )
    .bind(OffsetDateTime::from(left))
    .bind(OffsetDateTime::from(right))
    .bind(now)
    .bind(visible_until)
    .fetch_optional(&mut **txn)
    .await?)
}

async fn valid_events(
    txn: &mut Transaction<'_, Postgres>,
    events: &Value,
) -> Result<Option<bool>, DiaryWriteError> {
    if events.is_null() || events.as_object().is_some_and(Map::is_empty) {
        return Ok(None);
    }
    Ok(Some(sqlx::query_scalar(
        "SELECT NOT EXISTS(
             SELECT FROM jsonb_each($1::jsonb) AS expected(id,revision)
             WHERE NOT EXISTS(SELECT FROM events
                 WHERE id=expected.id::uuid AND NOT deleted AND to_jsonb(revision)=expected.revision)
         )",
    ).bind(events.to_string()).fetch_one(&mut **txn).await?))
}

fn instant(value: &Value) -> Result<OffsetDateTime, DiaryWriteError> {
    StoredInstant::legacy_input_with_offset(value).ok_or(DiaryWriteError::InvalidRecordedState)
}
