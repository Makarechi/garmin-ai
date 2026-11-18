use std::{future::Future, time::Duration};

use ga_domain::{event_input::source_calendar_date, timestamp::StoredInstant};
use serde_json::{Map, Value, json};
use sqlx::{Connection, PgConnection, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    DiaryQuestionKind, NotificationDecision, NotificationExclusions, NotificationPolicy,
    NotificationReason, ProactiveControlError, ReservedDiaryQuestion,
    diary_questions::{
        MIGRAINE_DETAILS, MigraineQuestionDetails, diary_question_packs, diary_question_timezone,
        migraine_question_text,
    },
    policy::{decision_in, lock_notification_policy},
};
use crate::{
    Database, OWNER_ENROLLMENT_LOCK, STORAGE_LIFECYCLE_LOCK,
    channel_ingress::telegram_poll::CHANNEL_SELECTION_LOCK,
    check_readiness,
    diary::caffeine_answer_at,
    outbox_claims::ReplyDestination,
    replay::REPLAY_MUTATION_LOCK,
    runtime_lease::RuntimeLease,
    share_policy::CHANNEL_CONSENT_LOCK,
    telegram_binding::{CHANNEL_BINDING_LOCK, TelegramBindingError, confirmed_pairing},
};

const STORAGE_DEADLINE: Duration = Duration::from_millis(1500);
const PROVIDER_DEADLINE: Duration = Duration::from_secs(60);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiaryQuestionOutcome {
    Accepted(i64),
    RetryAt(StoredInstant),
    Uncertain,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QuestionDispatchResult {
    Unavailable,
    Cancelled,
    Deferred(StoredInstant),
    Sent,
    Uncertain,
}

enum Preparation {
    Ready {
        key: String,
        channel: String,
        text: String,
        formatted: bool,
    },
    Finished(QuestionDispatchResult),
}

impl Database {
    /// Guard one private Telegram notification through its provider attempt and recorded outcome.
    pub async fn dispatch_diary_question<F>(
        &self,
        destination: ReplyDestination<'_>,
        expected_chat: i64,
        reserved: &ReservedDiaryQuestion,
        settings: &NotificationPolicy<'_>,
        now: StoredInstant,
        send: impl FnOnce(String) -> F,
    ) -> Result<QuestionDispatchResult, ProactiveControlError>
    where
        F: Future<Output = DiaryQuestionOutcome>,
    {
        if destination.channel != "telegram" {
            return Ok(QuestionDispatchResult::Unavailable);
        }
        let (mut connection, ready) = tokio::time::timeout(STORAGE_DEADLINE, async {
            let mut connection =
                PgConnection::connect_with(self.work.connect_options().as_ref()).await?;
            for lock in [
                STORAGE_LIFECYCLE_LOCK,
                OWNER_ENROLLMENT_LOCK,
                REPLAY_MUTATION_LOCK,
                CHANNEL_CONSENT_LOCK,
                CHANNEL_SELECTION_LOCK,
                CHANNEL_BINDING_LOCK,
            ] {
                let held: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock_shared($1)")
                    .bind(lock)
                    .fetch_one(&mut connection)
                    .await?;
                if !held {
                    return Err(ProactiveControlError::Busy);
                }
            }
            let exclusive: bool = sqlx::query_scalar(
                "SELECT pg_try_advisory_lock(hashtextextended('outbox:question:' || $1::text, 0))",
            )
            .bind(reserved.id.to_string())
            .fetch_one(&mut connection)
            .await?;
            if !exclusive {
                return Err(ProactiveControlError::Busy);
            }
            let mut txn = connection.begin().await?;
            prepare_storage(&mut txn).await?;
            let ready = prepare(
                &mut txn,
                &destination,
                expected_chat,
                reserved,
                settings,
                now,
            )
            .await?;
            txn.commit().await?;
            Ok((connection, ready))
        })
        .await
        .map_err(|_| ProactiveControlError::Database)??;
        let (key, channel, text, formatted) = match ready {
            Preparation::Ready {
                key,
                channel,
                text,
                formatted,
            } => (key, channel, text, formatted),
            Preparation::Finished(result) => return Ok(result),
        };
        let outcome = tokio::time::timeout(PROVIDER_DEADLINE, send(text))
            .await
            .unwrap_or(DiaryQuestionOutcome::Uncertain);
        let persisted = tokio::time::timeout(STORAGE_DEADLINE, async {
            finish(
                &mut connection,
                &key,
                &channel,
                formatted,
                reserved,
                now,
                outcome,
            )
            .await
        })
        .await;
        Ok(match persisted {
            Ok(Ok(result)) => result,
            // The stored sending marker makes this ambiguous on restart.
            _ => QuestionDispatchResult::Uncertain,
        })
    }
}

impl RuntimeLease {
    /// Resolve legacy or native sends after restart, while holding the exclusive worker lease.
    pub async fn recover_diary_question_deliveries(
        &mut self,
    ) -> Result<u64, ProactiveControlError> {
        tokio::time::timeout(STORAGE_DEADLINE, async {
            let mut txn = self.connection.begin().await?;
            prepare_storage(&mut txn).await?;
            lock_notification_policy(&mut txn).await?;
            let repaired = sqlx::query(
                "WITH interrupted AS (
                   SELECT q.id,marker.value->>'status' AS provider_status,marker.key IS NOT NULL AS started
                   FROM pending_questions q LEFT JOIN app_state marker
                     ON marker.key='outbox:question:' || q.id::text || ':0'
                   WHERE q.status='sending' AND q.kind IN ('migraine','caffeine')
                 )
                 UPDATE pending_questions q SET
                   status=CASE WHEN NOT p.started OR p.provider_status='pending' THEN 'pending'
                               WHEN p.provider_status='sent' THEN 'sent' ELSE 'uncertain' END,
                   sent_at=CASE WHEN NOT p.started OR p.provider_status='pending' THEN NULL ELSE q.sent_at END
                 FROM interrupted p WHERE p.id=q.id AND q.status='sending'",
            )
            .execute(&mut *txn)
            .await?
            .rows_affected();
            txn.commit().await?;
            Ok(repaired)
        })
        .await
        .map_err(|_| ProactiveControlError::Database)?
    }
}

async fn prepare_storage(txn: &mut Transaction<'_, Postgres>) -> Result<(), ProactiveControlError> {
    sqlx::query("SET LOCAL statement_timeout='750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn)
        .await
        .map_err(ProactiveControlError::Storage)
}

async fn prepare(
    txn: &mut Transaction<'_, Postgres>,
    destination: &ReplyDestination<'_>,
    expected_chat: i64,
    reserved: &ReservedDiaryQuestion,
    settings: &NotificationPolicy<'_>,
    now: StoredInstant,
) -> Result<Preparation, ProactiveControlError> {
    use QuestionDispatchResult::{Cancelled, Deferred, Sent, Unavailable, Uncertain};

    let finished = |result| Ok(Preparation::Finished(result));
    let Some((kind, event, stored, raw, expires)) =
        sqlx::query_as::<_, (String, Option<Uuid>, String, String, OffsetDateTime)>(
            "SELECT kind,event_id,text,evidence::text,expires_at FROM pending_questions
         WHERE id=$1 AND status='sending' AND attempts=$2 AND sent_at=$3 FOR UPDATE NOWAIT",
        )
        .bind(reserved.id)
        .bind(reserved.attempt)
        .bind(OffsetDateTime::from(reserved.reserved_at))
        .fetch_optional(&mut **txn)
        .await?
    else {
        return finished(Unavailable);
    };
    let key = format!("outbox:question:{}:0", reserved.id);
    let prior: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(&key)
            .fetch_optional(&mut **txn)
            .await?;
    let prior: Option<Value> = prior
        .map(|json| serde_json::from_str(&json))
        .transpose()
        .map_err(|_| ProactiveControlError::InvalidStoredState)?;
    if let Some(prior) = prior.as_ref()
        && prior["status"] != "pending"
    {
        let (state, result) = if prior["status"] == "sent" {
            ("sent", Sent)
        } else {
            ("uncertain", Uncertain)
        };
        set_question_state(txn, reserved.id, state).await?;
        return finished(result);
    }
    let kind_match = matches!(
        (kind.as_str(), reserved.kind),
        ("migraine", DiaryQuestionKind::Migraine) | ("caffeine", DiaryQuestionKind::Caffeine),
    );
    if !kind_match || event != reserved.event_id {
        return finished(Unavailable);
    }
    let channel = format!("telegram:{}", destination.instance_id);
    let zone = diary_question_timezone(txn, destination.owner_id, settings.timezone).await?;
    let policy = NotificationPolicy {
        timezone: &zone,
        daily_budget: None,
        destination: Some(&channel),
        exclusions: NotificationExclusions::default(),
        ..*settings
    };
    match decision_in(txn, &policy, now).await? {
        NotificationDecision::Cancel(cause) => {
            release(
                txn,
                reserved.id,
                "cancelled",
                None,
                cause == NotificationReason::OwnerPaused,
            )
            .await?;
            return finished(Cancelled);
        }
        NotificationDecision::Defer { retry_at, .. } => {
            release(txn, reserved.id, "pending", Some(retry_at), false).await?;
            return finished(Deferred(retry_at));
        }
        NotificationDecision::Allow => {}
    }
    let primary: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS (SELECT 1 FROM app_state WHERE key='preferences:onboarding')
            OR EXISTS(SELECT 1 FROM app_state WHERE key='preferences:onboarding'
                AND value->'channel'=jsonb_build_object('channel','telegram','instance_id',$1::text))",
    ).bind(destination.instance_id).fetch_one(&mut **txn).await?;
    let pairing = confirmed_pairing(txn, destination.instance_id, expected_chat).await;
    if matches!(pairing, Err(TelegramBindingError::Busy)) {
        return Err(ProactiveControlError::Busy);
    }
    let local_day =
        |at| source_calendar_date(at, &zone).ok_or(ProactiveControlError::InvalidStoredState);
    if !primary
        || !matches!(pairing, Ok(owner) if owner == destination.owner_id)
        || local_day(now)? != local_day(reserved.reserved_at)?
    {
        release(txn, reserved.id, "pending", None, false).await?;
        return finished(Unavailable);
    }
    let (migraine, caffeine) = diary_question_packs(txn, destination.owner_id).await?;
    let allowed = match reserved.kind {
        DiaryQuestionKind::Migraine => migraine,
        DiaryQuestionKind::Caffeine => caffeine,
    };
    let now_at = OffsetDateTime::from(now);
    if !allowed || expires <= now_at {
        release(txn, reserved.id, "cancelled", None, false).await?;
        return finished(Cancelled);
    }
    let text = match reserved.kind {
        DiaryQuestionKind::Migraine => {
            let event = event.ok_or(ProactiveControlError::InvalidStoredState)?;
            let details: Option<MigraineQuestionDetails> = sqlx::query_as(MIGRAINE_DETAILS)
                .bind(now_at)
                .bind(event)
                .fetch_optional(&mut **txn)
                .await?;
            let Some(details) = details else {
                release(txn, reserved.id, "cancelled", None, false).await?;
                return finished(Cancelled);
            };
            if details.2.is_some_and(|end| end <= now_at) {
                answer(txn, reserved.id, event).await?;
                return finished(Cancelled);
            }
            if !(now_at - time::Duration::days(2)..=now_at - time::Duration::hours(2))
                .contains(&details.1)
            {
                release(txn, reserved.id, "cancelled", None, false).await?;
                return finished(Cancelled);
            }
            migraine_question_text(&details)?
        }
        DiaryQuestionKind::Caffeine => {
            let evidence: Map<String, Value> = serde_json::from_str(&raw)
                .map_err(|_| ProactiveControlError::InvalidStoredState)?;
            if let Some(event) =
                caffeine_answer_at(txn, &evidence, reserved.reserved_at.into(), now_at).await?
            {
                answer(txn, reserved.id, event).await?;
                return finished(Cancelled);
            }
            stored
        }
    };
    if let Some(retry) = prior.as_ref().and_then(|prior| prior.get("retry_at"))
        && !retry.is_null()
    {
        let retry_at = StoredInstant::from_legacy_input(retry)
            .ok_or(ProactiveControlError::InvalidStoredState)?;
        if retry_at > now {
            release(txn, reserved.id, "pending", Some(retry_at), false).await?;
            return finished(Deferred(retry_at));
        }
    }
    let formatted = prior.as_ref().is_none_or(|item| item["formatted"] == true);
    write_marker(txn, &key, json!({
        "status":"sending","started_at":now.to_string(),"formatted":formatted,"channel_instance_id":channel,
    })).await?;
    sqlx::query("UPDATE pending_questions SET text=$2 WHERE id=$1")
        .bind(reserved.id)
        .bind(&text)
        .execute(&mut **txn)
        .await?;
    Ok(Preparation::Ready {
        key,
        channel,
        text,
        formatted,
    })
}

async fn finish(
    connection: &mut PgConnection,
    key: &str,
    channel: &str,
    formatted: bool,
    reserved: &ReservedDiaryQuestion,
    started_at: StoredInstant,
    outcome: DiaryQuestionOutcome,
) -> Result<QuestionDispatchResult, ProactiveControlError> {
    let mut txn = connection.begin().await?;
    prepare_storage(&mut txn).await?;
    let matching: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM pending_questions q JOIN app_state marker ON marker.key=$2
          WHERE q.id=$1 AND q.status='sending' AND q.attempts=$3 AND q.sent_at=$4
            AND marker.value->>'started_at'=$5 AND marker.value->>'status'='sending')",
    )
    .bind(reserved.id)
    .bind(key)
    .bind(reserved.attempt)
    .bind(OffsetDateTime::from(reserved.reserved_at))
    .bind(started_at.to_string())
    .fetch_one(&mut *txn)
    .await?;
    if !matching {
        return Ok(QuestionDispatchResult::Uncertain);
    }
    let mut marker = json!({"formatted":formatted,"channel_instance_id":channel});
    let result = match outcome {
        DiaryQuestionOutcome::Accepted(message_id) if message_id > 0 => {
            marker["status"] = json!("sent");
            marker["message_id"] = json!(message_id);
            marker["kind"] = json!("diary");
            set_question_state(&mut txn, reserved.id, "sent").await?;
            QuestionDispatchResult::Sent
        }
        DiaryQuestionOutcome::RetryAt(retry_at) => {
            let retry_at = retry_at.max(started_at);
            marker["status"] = json!("pending");
            marker["retry_at"] = json!(retry_at.to_string());
            release(&mut txn, reserved.id, "pending", Some(retry_at), false).await?;
            QuestionDispatchResult::Deferred(retry_at)
        }
        _ => {
            marker["status"] = json!("uncertain");
            set_question_state(&mut txn, reserved.id, "uncertain").await?;
            QuestionDispatchResult::Uncertain
        }
    };
    write_marker(&mut txn, key, marker).await?;
    txn.commit().await?;
    Ok(result)
}

async fn write_marker(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    value: Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=clock_timestamp()",
    )
    .bind(key)
    .bind(value.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn release(
    txn: &mut Transaction<'_, Postgres>,
    question: Uuid,
    state: &'static str,
    retry_at: Option<StoredInstant>,
    owner_paused: bool,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE pending_questions SET status=$2,sent_at=NULL,earliest_send_at=greatest(earliest_send_at,$3),
         evidence=CASE WHEN $4 THEN evidence || '{\"cancel_reason\":\"owner_pause\"}'::jsonb ELSE evidence END
         WHERE id=$1",
    ).bind(question).bind(state).bind(retry_at.map(OffsetDateTime::from)).bind(owner_paused)
      .execute(&mut **txn).await?;
    Ok(())
}

async fn answer(
    txn: &mut Transaction<'_, Postgres>,
    question: Uuid,
    event: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE pending_questions SET status='answered',sent_at=NULL,
         evidence=evidence || jsonb_build_object('answer_event_id',$2::text) WHERE id=$1",
    )
    .bind(question)
    .bind(event.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn set_question_state(
    txn: &mut Transaction<'_, Postgres>,
    question: Uuid,
    state: &'static str,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE pending_questions SET status=$2 WHERE id=$1")
        .bind(question)
        .bind(state)
        .execute(&mut **txn)
        .await?;
    Ok(())
}
