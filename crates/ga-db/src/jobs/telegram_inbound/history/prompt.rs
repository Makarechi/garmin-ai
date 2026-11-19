use ga_domain::{
    channel::{InboundEnvelope, InboundKind},
    timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::{Duration, OffsetDateTime};

use super::{ConversationBinding, TelegramDiaryCommitError, TelegramInboundLease};
use crate::{
    diary::{ClarificationClock, pending_clarification_in},
    outbox_enqueue::QueueIntentError,
    replay::REPLAY_MUTATION_LOCK,
};

pub(in crate::jobs::telegram_inbound) struct PromptState {
    pub(in crate::jobs::telegram_inbound) notice: Option<&'static str>,
    pub(in crate::jobs::telegram_inbound) draft: Option<SetupDraft>,
    pub(in crate::jobs::telegram_inbound) pending: bool,
    pub(in crate::jobs::telegram_inbound) guided_form: bool,
    pub(in crate::jobs::telegram_inbound) legacy_tracker: bool,
}

pub(in crate::jobs::telegram_inbound) struct SetupDraft {
    pub(in crate::jobs::telegram_inbound) key: String,
    pub(in crate::jobs::telegram_inbound) russian: bool,
    pub(in crate::jobs::telegram_inbound) awaiting_name: bool,
    pub(in crate::jobs::telegram_inbound) predates_step: bool,
}

pub(in crate::jobs::telegram_inbound) async fn blocked(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    at: StoredInstant,
) -> Result<Option<&'static str>, TelegramDiaryCommitError> {
    Ok(inspect(txn, binding, lease, envelope, at).await?.notice)
}

pub(in crate::jobs::telegram_inbound) async fn inspect(
    txn: &mut Transaction<'_, Postgres>,
    binding: ConversationBinding,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    at: StoredInstant,
) -> Result<PromptState, TelegramDiaryCommitError> {
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy));
    }
    let received = envelope.received_at();
    let message = envelope.occurred_at().unwrap_or(received);
    let clock = OffsetDateTime::from(at);
    let epoch: i64 = sqlx::query_scalar(
        "SELECT coalesce((payload->>'ordering_epoch')::bigint,0) FROM jobs WHERE id=$1",
    )
    .bind(lease.job_id)
    .fetch_one(&mut **txn)
    .await?;
    let update: i64 = envelope
        .external_event_id()
        .parse()
        .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
    let order = (epoch, update);
    let conversation_clock = ClarificationClock {
        message_at: StoredInstant::try_from(message)
            .map_err(|_| TelegramDiaryCommitError::Database)?,
        conversation_now: at,
        use_message_time: false,
    };
    let (destination, mut pending) = pending_clarification_in(txn, binding, conversation_clock)
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    if pending.is_none() {
        (_, pending) = pending_clarification_in(
            txn,
            binding,
            ClarificationClock {
                use_message_time: true,
                ..conversation_clock
            },
        )
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    }
    if pending.as_ref().is_some_and(|form| {
        form["button"] == "tracker_select" || form["definition_version_id"].is_string()
    }) && super::super::analysis_reply::is_analytic_reply(txn, envelope).await?
    {
        pending = None;
    }
    let guided_form = pending.as_ref().is_some_and(|form| {
        form["definition_version_id"].is_string()
            || form["chat_form"].is_object()
            || form["chat_close"].is_object()
            || form["button"].as_str().is_some_and(|button| {
                button == "tracker_select" || form["explicit_selector"] != true
            })
    });
    let legacy_tracker = pending.as_ref().is_some_and(|form| {
        form["definition_version_id"].is_string()
            && !form["chat_form"].is_object()
            && !form["chat_close"].is_object()
    });
    let key = format!("tracker:chat-setup:{destination}");
    let draft: Option<(String, OffsetDateTime)> =
        sqlx::query_as("SELECT value::text,updated_at FROM app_state WHERE key=$1")
            .bind(&key)
            .fetch_optional(&mut **txn)
            .await?;
    let mut setup = None;
    if let Some((value, updated)) = draft {
        let draft: Value =
            serde_json::from_str(&value).map_err(|_| TelegramDiaryCommitError::Database)?;
        let started = stamp(&draft["started_at"]);
        let activity = match &draft["last_activity_at"] {
            Value::Null => Some(updated),
            Value::String(value) if value.is_empty() => Some(updated),
            value => stamp(value),
        };
        let predates = draft["started_provider_update_id"]
            .as_i64()
            .is_some_and(|update| {
                order
                    < (
                        draft["started_ordering_epoch"].as_i64().unwrap_or(0),
                        update,
                    )
            });
        let active = |now| {
            started.is_none_or(|started| started <= now)
                && activity.is_some_and(|activity| activity >= now - Duration::hours(24))
        };
        if !predates && active(message) {
            let last_update = draft["last_provider_update_id"]
                .as_i64()
                .or_else(|| draft["started_provider_update_id"].as_i64());
            let last_epoch = draft["last_ordering_epoch"]
                .as_i64()
                .or_else(|| draft["started_ordering_epoch"].as_i64())
                .unwrap_or(0);
            let russian = draft["locale"]
                .as_str()
                .unwrap_or("ru")
                .split('-')
                .next()
                .is_some_and(|code| code.eq_ignore_ascii_case("ru"));
            setup = Some(SetupDraft {
                key,
                russian,
                awaiting_name: draft["name"].is_null(),
                predates_step: last_update.is_some_and(|last| order <= (last_epoch, last)),
            });
        } else if active(clock) {
            return Ok(PromptState {
                notice: Some(
                    "Сообщение отправлено до открытия текущего черновика. Откройте актуальное меню.",
                ),
                draft: None,
                pending: pending.is_some(),
                guided_form,
                legacy_tracker,
            });
        }
    }
    if pending.as_ref().is_some_and(|pending| {
        (pending["button"] == "tracker_select"
            || !pending["chat_close"].is_null()
            || !pending["chat_form"].is_null())
            && predates_prompt(pending, order, message, received)
    }) {
        return Ok(PromptState {
            notice: Some("Сообщение отправлено до текущего выбора. Откройте актуальное меню."),
            draft: setup,
            pending: true,
            guided_form,
            legacy_tracker,
        });
    }
    Ok(PromptState {
        notice: setup
            .as_ref()
            .filter(|_| envelope.kind() == InboundKind::Action)
            .map(|draft| {
                if draft.russian {
                    "Сначала завершите настройку трекера или отправьте /cancel."
                } else {
                    "Finish tracker setup or use /cancel before opening another form."
                }
            }),
        draft: setup,
        pending: pending.is_some(),
        guided_form,
        legacy_tracker,
    })
}

fn predates_prompt(
    pending: &Value,
    order: (i64, i64),
    message: OffsetDateTime,
    received: OffsetDateTime,
) -> bool {
    if stamp(&pending["prompt_advanced_at"]).is_some_and(|at| received <= at) {
        return true;
    }
    if let Some([epoch, update]) = pending["prompt_order"].as_array().map(Vec::as_slice)
        && let (Some(epoch), Some(update)) = (epoch.as_i64(), update.as_i64())
    {
        return order <= (epoch, update);
    }
    stamp(&pending["created_at"])
        .is_some_and(|created| message.unix_timestamp() < created.unix_timestamp())
}

fn stamp(value: &Value) -> Option<OffsetDateTime> {
    StoredInstant::from_legacy_input(value).map(OffsetDateTime::from)
}
