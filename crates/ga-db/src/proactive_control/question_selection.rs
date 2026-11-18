use std::time::Duration;

use ga_domain::timestamp::StoredInstant;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    NotificationDecision, NotificationExclusions, NotificationPolicy, ProactiveControlError,
    diary_questions::{
        MIGRAINE_DETAILS, MigraineQuestionDetails, diary_question_packs, diary_question_timezone,
        migraine_question_text,
    },
    policy::decision_in,
};
use crate::Database;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiaryQuestionKind {
    Migraine,
    Caffeine,
}

#[derive(Debug, Eq, PartialEq)]
pub struct ReservedDiaryQuestion {
    pub id: Uuid,
    pub kind: DiaryQuestionKind,
    pub text: String,
    pub event_id: Option<Uuid>,
    pub attempt: i32,
    pub reserved_at: StoredInstant,
}

impl Database {
    /// Reserve the next eligible diary question while checking the shared notification policy.
    pub async fn reserve_diary_question(
        &self,
        owner: Uuid,
        settings: &NotificationPolicy<'_>,
        now: StoredInstant,
    ) -> Result<Option<ReservedDiaryQuestion>, ProactiveControlError> {
        tokio::time::timeout(Duration::from_secs(5), async {
            let mut txn = self.begin_diary_question_policy().await?;
            let timezone = diary_question_timezone(&mut txn, owner, settings.timezone).await?;
            let policy = NotificationPolicy {
                timezone: &timezone,
                exclusions: NotificationExclusions::default(),
                ..*settings
            };
            if decision_in(&mut txn, &policy, now).await? != NotificationDecision::Allow {
                return Ok(None);
            }
            let question = reserve(&mut txn, owner, now).await?;
            txn.commit().await?;
            Ok(question)
        })
        .await
        .map_err(|_| ProactiveControlError::Database)?
    }
}

async fn reserve(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    reserved_at: StoredInstant,
) -> Result<Option<ReservedDiaryQuestion>, ProactiveControlError> {
    let now = OffsetDateTime::from(reserved_at);
    let (migraine_enabled, caffeine_enabled) = diary_question_packs(txn, owner).await?;
    let mut after = (None::<f64>, None::<Uuid>);
    loop {
        let candidates: Vec<(Uuid, String, String, Option<Uuid>, f64)> = sqlx::query_as(
            "SELECT id,kind,text,event_id,priority FROM pending_questions
             WHERE status='pending' AND kind IN ('migraine','caffeine') AND priority>=0.6
               AND earliest_send_at<=$1 AND expires_at>$1
               AND ($2::double precision IS NULL OR priority<$2 OR (priority=$2 AND id>$3))
             ORDER BY priority DESC,id LIMIT 32 FOR UPDATE SKIP LOCKED",
        )
        .bind(now)
        .bind(after.0)
        .bind(after.1)
        .fetch_all(&mut **txn)
        .await?;
        if candidates.is_empty() {
            return Ok(None);
        }
        for (id, kind, text, event_id, priority) in candidates {
            after = (Some(priority), Some(id));
            let (kind_code, enabled) = match kind.as_str() {
                "migraine" => (DiaryQuestionKind::Migraine, migraine_enabled),
                "caffeine" => (DiaryQuestionKind::Caffeine, caffeine_enabled),
                _ => return Err(ProactiveControlError::InvalidStoredState),
            };
            if !enabled {
                retire(txn, id, "cancelled").await?;
                continue;
            }
            let mut episode = None;
            if let Some(event_id) = event_id {
                let details: Option<MigraineQuestionDetails> = sqlx::query_as(MIGRAINE_DETAILS)
                    .bind(now)
                    .bind(event_id)
                    .fetch_optional(&mut **txn)
                    .await?;
                let Some(details) = details.filter(|details| details.1 <= now) else {
                    retire(txn, id, "cancelled").await?;
                    continue;
                };
                if details.2.is_some_and(|end| end <= now) {
                    retire(txn, id, "answered").await?;
                    continue;
                }
                if !(now - time::Duration::days(2)..=now - time::Duration::hours(2))
                    .contains(&details.1)
                {
                    retire(txn, id, "cancelled").await?;
                    continue;
                }
                episode = Some(details);
            }
            let recent: bool = sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM pending_questions
                   WHERE kind=$1 AND sent_at >= $2::timestamptz - interval '24 hours')",
            )
            .bind(&kind)
            .bind(now)
            .fetch_one(&mut **txn)
            .await?;
            if recent {
                continue;
            }
            let text = match kind_code {
                DiaryQuestionKind::Migraine => migraine_question_text(
                    episode
                        .as_ref()
                        .ok_or(ProactiveControlError::InvalidStoredState)?,
                )?,
                DiaryQuestionKind::Caffeine => text,
            };
            let attempt: i32 = sqlx::query_scalar(
                "UPDATE pending_questions SET text=$2,status='sending',sent_at=$3,
                     expires_at=$3 + interval '48 hours',attempts=attempts+1
                 WHERE id=$1 RETURNING attempts",
            )
            .bind(id)
            .bind(&text)
            .bind(now)
            .fetch_one(&mut **txn)
            .await?;
            return Ok(Some(ReservedDiaryQuestion {
                id,
                kind: kind_code,
                text,
                event_id,
                attempt,
                reserved_at,
            }));
        }
    }
}

async fn retire(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    state: &'static str,
) -> Result<(), ProactiveControlError> {
    sqlx::query("UPDATE pending_questions SET status=$2 WHERE id=$1")
        .bind(id)
        .bind(state)
        .execute(&mut **txn)
        .await?;
    Ok(())
}
