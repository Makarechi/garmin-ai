pub(crate) mod analysis_reply;
mod control;
mod diary;
mod history;
mod local_text;
mod model_correction;
mod model_publication;
mod model_reply;
mod tracker_form;
mod tracker_setup;

pub use control::TelegramControlError;
pub use diary::TelegramDiaryCommitError;
pub(crate) use history::resolve_legacy_history;
pub use local_text::{
    LocalTelegramText, SetupMetadata, TelegramModelAdmission, TelegramSafetyDecision,
};
pub use model_publication::TelegramModelContext;
pub use model_reply::{ModelContinuation, ModelContinuationReply};

use std::time::Duration;

use ga_domain::{telegram_control::COMMAND_WHITESPACE, timestamp::StoredInstant};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    ATTEMPT_LIMIT, FailureReason, JobOutcome, JobStoreError, begin, fail_exhausted,
    finish_in_transaction,
};
use crate::{
    Database,
    channel_ingress::native_telegram::{InboxLane, KIND},
    telegram_cursor::{TelegramCursorError, lock_ingress},
};

#[derive(Clone, Copy)]
pub struct TelegramInboundTarget<'a> {
    pub owner_id: Uuid,
    pub instance_id: &'a str,
}

pub struct TelegramInboundLease {
    job_id: Uuid,
    message_id: Uuid,
    operation_id: Uuid,
    token: Uuid,
    envelope: String,
}

impl TelegramInboundLease {
    pub fn message_id(&self) -> Uuid {
        self.message_id
    }

    pub fn operation_id(&self) -> Uuid {
        self.operation_id
    }

    pub fn job_identity(&self) -> (Uuid, Uuid) {
        (self.job_id, self.token)
    }

    pub fn envelope_json(&self) -> &str {
        &self.envelope
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelegramInboundState {
    Processed,
    Invalid,
}

impl TelegramInboundState {
    fn label(self) -> &'static str {
        match self {
            Self::Processed => "processed",
            Self::Invalid => "invalid",
        }
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum InboxWork {
    All,
    Controls,
    LocalDiary,
    ScreenText,
    TextWithoutProvider,
    ModelText,
    TrackerSetup,
    TrackerForm,
}

impl InboxWork {
    fn commands(self) -> Option<&'static [&'static str]> {
        match self {
            Self::Controls => Some(&[
                "/forget_conversation",
                "/today",
                "/status",
                "/goals",
                "/pause",
                "/resume",
                "/help",
                "/start",
                "/debug",
            ]),
            Self::LocalDiary => Some(&["/history", "/cancel", "/undo"]),
            _ => None,
        }
    }

    fn text_stage(self) -> i32 {
        match self {
            Self::ScreenText => 1,
            Self::TextWithoutProvider => 2,
            Self::ModelText => 3,
            Self::TrackerSetup => 4,
            Self::TrackerForm => 5,
            _ => 0,
        }
    }
}

impl Database {
    /// Reserve the earliest native delivery for a confirmed owner and bot.
    /// Later diary work waits behind a running or delayed earlier delivery.
    pub async fn claim_telegram_inbound(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(target, now, lease_seconds, InboxLane::Diary, InboxWork::All)
            .await
    }

    /// Claim short owner controls independently of long diary or provider work.
    pub async fn claim_telegram_control(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(
            target,
            now,
            lease_seconds,
            InboxLane::Control,
            InboxWork::All,
        )
        .await
    }

    /// Claim commands served by the current local worker, for historical and new inbox rows.
    pub async fn claim_local_telegram_control(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(
            target,
            now,
            lease_seconds,
            InboxLane::Control,
            InboxWork::Controls,
        )
        .await
    }

    /// Claim commands and selectors served by the local diary worker.
    pub async fn claim_local_telegram_history(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(
            target,
            now,
            lease_seconds,
            InboxLane::Diary,
            InboxWork::LocalDiary,
        )
        .await
    }

    /// Screen the next unchecked text or voice caption without waiting on provider work.
    pub async fn claim_telegram_local_screen(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(
            target,
            now,
            lease_seconds,
            InboxLane::Diary,
            InboxWork::ScreenText,
        )
        .await
    }

    /// Resume the first screened report when no provider is configured and usable.
    pub async fn claim_telegram_no_model_text(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(
            target,
            now,
            lease_seconds,
            InboxLane::Diary,
            InboxWork::TextWithoutProvider,
        )
        .await
    }

    /// Claim the oldest authenticated text after it cleared local screening.
    pub async fn claim_telegram_model_text(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(
            target,
            now,
            lease_seconds,
            InboxLane::Diary,
            InboxWork::ModelText,
        )
        .await
    }

    /// Reserve an ordered setup command or a locally screened draft answer.
    pub async fn claim_telegram_tracker_setup(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(
            target,
            now,
            lease_seconds,
            InboxLane::Diary,
            InboxWork::TrackerSetup,
        )
        .await
    }

    pub async fn claim_telegram_tracker_form(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        self.claim_telegram_lane(
            target,
            now,
            lease_seconds,
            InboxLane::Diary,
            InboxWork::TrackerForm,
        )
        .await
    }

    async fn claim_telegram_lane(
        &self,
        target: TelegramInboundTarget<'_>,
        now: StoredInstant,
        lease_seconds: u32,
        lane: InboxLane,
        work: InboxWork,
    ) -> Result<Option<TelegramInboundLease>, JobStoreError> {
        if !(1..=86_400).contains(&lease_seconds) {
            return Err(JobStoreError::InvalidLeaseDuration);
        }
        if !(1..=200).contains(&target.instance_id.chars().count())
            || target.instance_id.contains('\0')
        {
            return Err(JobStoreError::InvalidRecordedState);
        }
        tokio::time::timeout(Duration::from_millis(1500), async {
            let pool = if lane == InboxLane::Control {
                &self.control
            } else {
                &self.work
            };
            let mut txn = begin(pool).await?;
            if !try_ingress_lock(&mut txn).await? {
                return Ok(None);
            }
            let now = OffsetDateTime::from(now);
            fail_exhausted(&mut txn, now, Some(&[KIND])).await?;
            sqlx::query(
                "WITH exhausted AS (
                     SELECT i.id FROM inbound_messages i
                     JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id)
                     WHERE i.owner_id=$1 AND i.channel='telegram' AND i.channel_instance_id=$2
                       AND i.status='pending' AND i.legacy_telegram_update_id IS NULL
                       AND j.kind=$3 AND j.status='failed' AND j.last_error='RetryLimitExceeded'
                     ORDER BY i.id LIMIT 100
                 )
                 UPDATE inbound_messages i SET status='invalid' FROM exhausted x WHERE i.id=x.id",
            )
            .bind(target.owner_id)
            .bind(target.instance_id)
            .bind(KIND)
            .execute(&mut *txn)
            .await?;
            let token = Uuid::new_v4();
            let found: Option<(Uuid, Uuid, Uuid, String)> = sqlx::query_as(
                concat!("WITH oldest AS (
                     SELECT j.id,i.id AS message_id,i.operation_id,i.envelope
                     FROM jobs j JOIN inbound_messages i
                       ON j.dedup_key=concat('telegram:inbound:native:',i.id)
                       AND j.payload->>'inbound_message_id'=i.id::text
                     CROSS JOIN LATERAL (
                         SELECT btrim(translate(i.envelope->>'text', $10, repeat(' ',char_length($10)))) AS input
                     ) command
                     WHERE j.kind=$1 AND j.status IN ('pending','running')
                       AND coalesce(j.payload->>'lane','diary')=$8
                       AND i.owner_id=$2 AND i.channel='telegram' AND i.channel_instance_id=$3
                       AND i.status='pending' AND i.legacy_telegram_update_id IS NULL
                       AND ($12 = 0 OR ($12 = 5 AND (
                          (i.kind='action' AND (starts_with(i.envelope#>>'{action,action_id}','create:')
                            OR split_part(i.envelope#>>'{action,action_id}','/',1)=ANY(ARRAY['diary.history.edit','diary.history.close'])
                              AND EXISTS(SELECT 1 FROM events e WHERE starts_with(e.kind,'user.')
                                AND NOT e.deleted AND e.id=CASE WHEN pg_input_is_valid(split_part(i.envelope#>>'{action,action_id}','/',2),'uuid')
                                  THEN split_part(i.envelope#>>'{action,action_id}','/',2)::uuid END
                                AND e.revision::text=split_part(i.envelope#>>'{action,action_id}','/',3))))
                          OR (i.kind='voice' AND coalesce(command.input,'')<>'' AND command.input NOT LIKE '/%'
                            AND j.payload->>'local_safety_checked'='true'
                            AND j.payload->>'tracker_caption_checked' IS DISTINCT FROM 'true')
                          OR (i.kind IN ('text','edit','voice') AND EXISTS(SELECT 1 FROM app_state p
                              WHERE p.key=CASE WHEN $3='primary' THEN 'conversation:pending' ELSE concat('conversation:pending:telegram:',$3) END
                                AND (jsonb_typeof(p.value->'chat_form')='object' OR jsonb_typeof(p.value->'chat_close')='object'
                                  OR (p.value->>'button'='tracker_select' AND (i.kind<>'voice'
                                    OR j.payload->>'tracker_caption_checked' IS DISTINCT FROM 'true')))
                                AND CASE WHEN pg_input_is_valid(coalesce(p.value->>'preset_selected_at',p.value->>'created_at'),'timestamptz')
                                  THEN (coalesce(p.value->>'preset_selected_at',p.value->>'created_at')::timestamptz
                                    BETWEEN clock_timestamp()-interval '2 hours' AND clock_timestamp())
                                    OR (coalesce(p.value->>'preset_selected_at',p.value->>'created_at')::timestamptz
                                      BETWEEN coalesce((i.envelope->>'occurred_at')::timestamptz,i.received_at)-interval '2 hours'
                                        AND coalesce((i.envelope->>'occurred_at')::timestamptz,i.received_at))
                                  ELSE false END)
                              AND (coalesce(command.input,'')='' OR (command.input LIKE '/%'
                                AND split_part(command.input,' ',1)<>ALL(ARRAY['/cancel','/undo','/newtracker','/history']))
                                OR i.kind='voice' AND j.payload->>'local_safety_checked'='true'))
                       )) OR ($12 = 4 AND i.kind IN ('text','edit','voice') AND (
                           split_part(command.input,' ',1)='/newtracker'
                           OR (EXISTS(SELECT 1 FROM app_state WHERE key=concat('tracker:chat-setup:telegram:',$3))
                             AND (split_part(command.input,' ',1)=ANY(ARRAY['/preview','/confirm_tracker','/privacy','/remove_field'])
                               OR (split_part(command.input,' ',1)='/cancel' AND command.input<>'/cancel')
                               OR (i.kind IN ('text','edit') AND coalesce(command.input,'')='')
                               OR (i.kind='voice' AND coalesce(command.input,'') NOT LIKE '/%'
                                 AND (j.payload->>'local_safety_checked' IS NOT DISTINCT FROM 'true'
                                   OR (i.kind='voice' AND coalesce(command.input,'')='')))))
                       )) OR ($12 BETWEEN 1 AND 3 AND (
                           i.kind IN ('text','edit','voice') AND ($12<>3 OR i.kind IN ('text','edit')) AND (
                             (command.input<>'' AND command.input NOT LIKE '/%'
                               AND (j.payload->>'local_safety_checked' IS NOT DISTINCT FROM 'true') = ($12 >= 2))
                             OR ($12 = 2 AND i.kind='voice' AND coalesce(command.input,'')='')
                           )
                       )))
                       AND (NOT $11 OR coalesce(split_part(command.input,' ',1),'')<>'/cancel' OR command.input='/cancel'
                           OR NOT EXISTS(SELECT 1 FROM app_state WHERE key=concat('tracker:chat-setup:telegram:',$3)))
                       AND ($9::text[] IS NULL OR (
                         (NOT $11 OR i.kind IN ('text','voice')) AND split_part(command.input,' ',1)=ANY($9))
                         OR ($11 AND i.kind='action' AND (
                           split_part(i.envelope#>>'{action,action_id}','/',1)=ANY(ARRAY[
                             'diary.history.page','diary.history.open','diary.history.delete','diary.history.expired'])
                           OR (split_part(i.envelope#>>'{action,action_id}','/',1)=ANY(ARRAY[
                             'diary.history.edit','diary.history.close'])
                             AND NOT EXISTS(SELECT 1 FROM events e WHERE starts_with(e.kind,'user.')
                               AND NOT e.deleted AND e.id=CASE
                                 WHEN pg_input_is_valid(split_part(i.envelope#>>'{action,action_id}','/',2),'uuid')
                                 THEN split_part(i.envelope#>>'{action,action_id}','/',2)::uuid END
                               AND e.revision::text=split_part(i.envelope#>>'{action,action_id}','/',3)))
                         )))
                       AND (
                         ($12 < 2 AND (NOT $11 OR coalesce(split_part(command.input,' ',1),'') NOT IN ('/cancel','/undo')))
                         OR (($12 >= 2 OR command.input IN ('/cancel','/undo')) AND ",
                         include_str!("telegram_inbound/no_prior_diary.sql"),
                         ")
                       )
                     ORDER BY coalesce((j.payload->>'ordering_epoch')::bigint,0),
                              i.external_event_id::bigint,j.id
                     LIMIT 1
                 )
                 UPDATE jobs j SET status='running', attempts=j.attempts+1,
                        lease_until=$4::timestamptz+$5*interval '1 second',lease_token=$6
                 FROM oldest WHERE j.id=oldest.id AND j.attempts<$7
                   AND ((j.status='pending' AND j.run_at<=$4)
                         OR (j.status='running' AND j.lease_until<$4))
                 RETURNING j.id,oldest.message_id,oldest.operation_id,oldest.envelope::text"),
            )
            .bind(KIND)
            .bind(target.owner_id)
            .bind(target.instance_id)
            .bind(now)
            .bind(lease_seconds as i32)
            .bind(token)
            .bind(ATTEMPT_LIMIT)
            .bind(lane.label())
            .bind(work.commands())
            .bind(COMMAND_WHITESPACE)
            .bind(work == InboxWork::LocalDiary)
            .bind(work.text_stage())
            .fetch_optional(&mut *txn)
            .await?;
            let mut result =
                found.map(
                    |(job_id, message_id, operation_id, envelope)| TelegramInboundLease {
                        job_id,
                        message_id,
                        operation_id,
                        token,
                        envelope,
                    },
                );
            if let Some(lease) = result.as_ref()
                && superseded(&mut txn, lease.message_id).await?
            {
                complete(&mut txn, lease, now, TelegramInboundState::Processed).await?;
                result = None;
            }
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| JobStoreError::Database)?
    }

    /// Keep the same message pending when a provider has advertised a later
    /// retry time. The job checks the current lease before returning its attempt.
    pub async fn defer_telegram_inbound(
        &self,
        lease: &TelegramInboundLease,
        now: StoredInstant,
        retry_at: StoredInstant,
    ) -> Result<(), JobStoreError> {
        if retry_at <= now {
            return Err(JobStoreError::InvalidRecordedState);
        }
        self.finish_job(
            lease.job_id,
            lease.token,
            KIND,
            now,
            JobOutcome::Failed {
                reason: FailureReason::ProviderCooldown,
                retry_at: Some(retry_at),
            },
        )
        .await?;
        Ok(())
    }

    /// Complete work that needs no diary mutation or generated reply. A worker
    /// that lost its lease cannot change either the job or inbox state.
    pub async fn finish_telegram_inbound(
        &self,
        lease: &TelegramInboundLease,
        now: StoredInstant,
        state: TelegramInboundState,
    ) -> Result<(), JobStoreError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = begin(&self.work).await?;
            if !try_ingress_lock(&mut txn).await? {
                return Err(JobStoreError::Database);
            }
            complete(&mut txn, lease, now.into(), state).await?;
            txn.commit().await?;
            Ok(())
        })
        .await
        .map_err(|_| JobStoreError::Database)?
    }

    /// Yield an unexpired model claim while the configured provider is cooling down.
    pub async fn defer_telegram_model_text(
        &self,
        lease: &TelegramInboundLease,
        now: StoredInstant,
        retry_at: StoredInstant,
    ) -> Result<(), JobStoreError> {
        self.finish_job(
            lease.job_id,
            lease.token,
            KIND,
            now,
            JobOutcome::Failed {
                reason: super::FailureReason::ProviderCooldown,
                retry_at: Some(retry_at),
            },
        )
        .await?;
        Ok(())
    }
}

async fn try_ingress_lock(txn: &mut Transaction<'_, Postgres>) -> Result<bool, JobStoreError> {
    match lock_ingress(txn).await {
        Ok(()) => Ok(true),
        Err(TelegramCursorError::Busy) => Ok(false),
        Err(_) => Err(JobStoreError::Database),
    }
}

async fn superseded(
    txn: &mut Transaction<'_, Postgres>,
    message_id: Uuid,
) -> Result<bool, JobStoreError> {
    Ok(sqlx::query_scalar(
        "SELECT EXISTS (
           SELECT 1 FROM inbound_messages i JOIN inbound_messages newer
             ON newer.owner_id=i.owner_id AND newer.conversation_id=i.conversation_id
             AND newer.external_message_id=i.external_message_id AND newer.revision>i.revision
           WHERE i.id=$1 AND i.kind IN ('text','voice','edit') AND newer.kind='edit'
         )",
    )
    .bind(message_id)
    .fetch_one(&mut **txn)
    .await?)
}

async fn complete(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    now: OffsetDateTime,
    state: TelegramInboundState,
) -> Result<(), JobStoreError> {
    finish_in_transaction(
        txn,
        lease.job_id,
        lease.token,
        KIND,
        Some(now),
        JobOutcome::Completed,
    )
    .await?;
    let done = sqlx::query(
        "UPDATE inbound_messages SET status=$2
         WHERE id=$1 AND status='pending' AND operation_id=$3 AND legacy_telegram_update_id IS NULL",
    )
    .bind(lease.message_id)
    .bind(state.label())
    .bind(lease.operation_id)
    .execute(&mut **txn)
    .await?;
    if done.rows_affected() != 1 {
        return Err(JobStoreError::LeaseLost);
    }
    Ok(())
}
