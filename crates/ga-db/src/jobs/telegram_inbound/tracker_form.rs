use std::time::Duration;

use ga_domain::{
    channel::{InboundEnvelope, InboundKind, OutboundIntent},
    event_payload::python_whitespace,
    event_schema::chat_form::{
        ChatFormError, ChatFormProgress, advance_chat_form, begin_chat_form,
        restart_chat_form_fields,
    },
    event_schema::{CustomEntryError, InvalidEntryValues},
    timestamp::{OffsetTimestamp, StoredInstant},
};
use serde_json::{Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    TelegramDiaryCommitError, TelegramInboundLease, TelegramInboundState, complete,
    history::prompt,
    local_text::{preferred_locale, verify_order},
};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{
        ClarificationClock,
        actions::{FormSubmission, FormSubmitError, TrackerFormWrite},
        cancel_in, pending_clarification_in, record_history_selection,
    },
    health_analysis::owner_timezone,
    outbox_enqueue::{QueueIntent, QueueIntentError, queue_outbox_intent},
    share_policy::{self, DestinationKind, ShareCategory, ShareDestination, ShareRequirement},
};

impl Database {
    pub async fn apply_telegram_tracker_form(
        &self,
        lease: &TelegramInboundLease,
        timezone: &str,
        locale: &str,
    ) -> Result<(), TelegramDiaryCommitError> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        tokio::time::timeout(Duration::from_millis(2500), async {
            let mut txn = self.begin_telegram_text(lease, &envelope).await?;
            let handled = apply_in(&mut txn, lease, &envelope, timezone, locale).await?;
            txn.commit().await?;
            if handled {
                Ok(())
            } else {
                Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy))
            }
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

pub(super) async fn apply_in(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    timezone: &str,
    configured_locale: &str,
) -> Result<bool, TelegramDiaryCommitError> {
    verify_order(txn, lease).await?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let now = StoredInstant::try_from(at).map_err(|_| TelegramDiaryCommitError::Database)?;
    let binding = ConversationBinding {
        owner_id: envelope.owner_id(),
        conversation_id: envelope.conversation_id(),
    };
    let locale = preferred_locale(txn, binding, configured_locale).await?;
    let clock = ClarificationClock {
        message_at: StoredInstant::try_from(
            envelope.occurred_at().unwrap_or(envelope.received_at()),
        )
        .map_err(|_| TelegramDiaryCommitError::Database)?,
        conversation_now: now,
        use_message_time: false,
    };
    let (_, mut pending) = pending_clarification_in(txn, binding, clock)
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    if pending.is_none() {
        (_, pending) = pending_clarification_in(
            txn,
            binding,
            ClarificationClock {
                use_message_time: true,
                ..clock
            },
        )
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    }
    let action = action_id(envelope);
    let active_form = pending.as_ref().is_some_and(|pending| {
        pending["chat_form"].is_object() || pending["chat_close"].is_object()
    });
    if action.is_none() && !active_form {
        return Ok(false);
    }
    let inspected = prompt::inspect(txn, binding, lease, envelope, now).await?;
    if let Some(notice) = inspected.notice {
        let notice = localized_notice(notice, &locale);
        write_reply(txn, lease, envelope, &notice, None).await?;
        return Ok(true);
    }
    if let Some((action, close)) = action {
        open(txn, lease, envelope, &locale, timezone, &action, close).await?;
        return Ok(true);
    }
    if envelope.kind() == InboundKind::Voice
        && envelope
            .text()
            .unwrap_or("")
            .trim_matches(python_whitespace)
            .is_empty()
    {
        write_reply(txn,lease,envelope,"Голосовые сообщения сейчас недоступны. Показатели доступны через /today, записи — через кнопки.",None).await?;
        return Ok(true);
    }
    let channel_locale = locale.clone();
    let Some(mut pending) = pending else {
        return Ok(false);
    };
    let closing = pending["chat_close"].is_object();
    let state = pending[if closing { "chat_close" } else { "chat_form" }].clone();
    let locale = state["locale"]
        .as_str()
        .ok_or(TelegramDiaryCommitError::Database)?
        .to_owned();
    let action = state["action_id"]
        .as_str()
        .ok_or(TelegramDiaryCommitError::Database)?;
    let form = match Database::tracker_generated_form_in(txn, envelope.owner_id(), action, &locale)
        .await
    {
        Ok(form) => form,
        Err(
            crate::diary::actions::TrackerActionError::Missing
            | crate::diary::actions::TrackerActionError::InactiveContract
            | crate::diary::actions::TrackerActionError::EntryChanged,
        ) => {
            cancel_in(txn, binding)
                .await
                .map_err(TelegramDiaryCommitError::Clarification)?;
            let text = changed(&locale, closing || action.starts_with("edit:"));
            write_reply(
                txn,
                lease,
                envelope,
                &with_safety(text, &channel_locale),
                None,
            )
            .await?;
            return Ok(true);
        }
        Err(_) => return Err(TelegramDiaryCommitError::Database),
    };
    let version = version(&form)?;
    let editing = closing || action.starts_with("edit:");
    if !allowed(txn, envelope, version, editing).await? {
        cancel_in(txn, binding)
            .await
            .map_err(TelegramDiaryCommitError::Clarification)?;
        write_reply(
            txn,
            lease,
            envelope,
            &with_safety(
                "Доступ к трекеру изменился. Откройте актуальное меню.",
                &channel_locale,
            ),
            None,
        )
        .await?;
        return Ok(true);
    }
    let text = envelope.text().unwrap_or("");
    let schema: String =
        sqlx::query_scalar("SELECT schema::text FROM event_definition_versions WHERE id=$1")
            .bind(version)
            .fetch_one(&mut **txn)
            .await?;
    let schema: Value =
        serde_json::from_str(&schema).map_err(|_| TelegramDiaryCommitError::Database)?;
    let progress = if closing {
        advance_close(&form, &state, text, clock.message_at.into())
    } else {
        advance_chat_form(&form, &schema, &state, text, clock.message_at.into())
    }
    .map_err(|_| TelegramDiaryCommitError::Database)?;
    let mut keep = true;
    let reply = match progress {
        ChatFormProgress::Continue { state, reply } => {
            pending[if closing { "chat_close" } else { "chat_form" }] = state;
            reply
        }
        ChatFormProgress::Cancelled(reply) => {
            keep = false;
            reply
        }
        ChatFormProgress::Submit {
            mut state,
            submission,
        } => {
            let form_submission = FormSubmission::parse(
                &serde_json::to_vec(&submission).map_err(|_| TelegramDiaryCommitError::Database)?,
            )
            .map_err(|_| TelegramDiaryCommitError::Database)?;
            let actor = format!("telegram:{}", envelope.sender_ref());
            let key = state["submission_id"]
                .as_str()
                .map(|nonce| format!("telegram-chat:{nonce}"));
            let mut change = txn.begin().await?;
            let result = Database::submit_tracker_form_in(
                &mut change,
                action,
                &form_submission,
                TrackerFormWrite {
                    owner_id: envelope.owner_id(),
                    actor: &actor,
                    source: Some("telegram_text"),
                    idempotency_key: key.as_deref(),
                },
            )
            .await;
            if result.is_ok() {
                change.commit().await?;
            } else {
                change.rollback().await?;
            }
            match result {
                Ok(_) => {
                    keep = false;
                    say(
                        &locale,
                        if closing {
                            "Запись завершена."
                        } else if editing {
                            "Запись исправлена."
                        } else {
                            "Запись сохранена."
                        },
                        if closing {
                            "Entry closed."
                        } else if editing {
                            "Entry updated."
                        } else {
                            "Entry saved."
                        },
                    )
                    .to_owned()
                }
                Err(FormSubmitError::Diary(crate::diary::DiaryWriteError::CustomInput(
                    CustomEntryError::Value(InvalidEntryValues::Limits),
                ))) if !closing => match restart_chat_form_fields(&form, &mut state, false) {
                    Ok(reply) => {
                        pending["chat_form"] = state;
                        reply
                    }
                    Err(ChatFormError::Answer(reply)) => {
                        keep = false;
                        reply
                    }
                    Err(_) => return Err(TelegramDiaryCommitError::Database),
                },
                Err(FormSubmitError::Validation(_)) if !closing => {
                    match restart_chat_form_fields(&form, &mut state, true) {
                        Ok(reply) => {
                            pending["chat_form"] = state;
                            reply
                        }
                        Err(ChatFormError::Answer(reply)) => {
                            keep = false;
                            reply
                        }
                        Err(_) => return Err(TelegramDiaryCommitError::Database),
                    }
                }
                Err(FormSubmitError::Validation(_)) => {
                    keep = false;
                    say(&locale,"Значения записи требуют исправления. Откройте /history и выберите «Исправить».","Entry values need correction. Open /history and choose Edit.").to_owned()
                }
                Err(
                    FormSubmitError::Action(
                        crate::diary::actions::TrackerActionError::Missing
                        | crate::diary::actions::TrackerActionError::InactiveContract
                        | crate::diary::actions::TrackerActionError::EntryChanged,
                    )
                    | FormSubmitError::Conflict(_),
                ) => {
                    keep = false;
                    changed(&locale, editing).to_owned()
                }
                Err(_) => return Err(TelegramDiaryCommitError::Database),
            }
        }
    };
    if keep {
        let processed: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut **txn)
            .await?;
        pending["created_at"] = json!(OffsetTimestamp(processed).to_string());
        pending["prompt_advanced_at"] = json!(OffsetTimestamp(processed).to_string());
        pending["prompt_order"] = order(txn, lease, envelope).await?;
        record_history_selection(txn, binding, pending)
            .await
            .map_err(TelegramDiaryCommitError::Clarification)?;
    } else {
        cancel_in(txn, binding)
            .await
            .map_err(TelegramDiaryCommitError::Clarification)?;
    }
    write_reply(
        txn,
        lease,
        envelope,
        &with_safety(&reply, &channel_locale),
        Some((version, editing)),
    )
    .await?;
    Ok(true)
}

fn action_id(envelope: &InboundEnvelope) -> Option<(String, bool)> {
    if envelope.kind() != InboundKind::Action {
        return None;
    }
    let document = envelope.document();
    let action = document["action"]["action_id"].as_str()?;
    if action
        .strip_prefix("create:")
        .is_some_and(|id| id.parse::<Uuid>().is_ok())
    {
        return Some((action.to_owned(), false));
    }
    let mut parts = action.split('/');
    let close = match parts.next()? {
        "diary.history.edit" => false,
        "diary.history.close" => true,
        _ => return None,
    };
    let id: Uuid = parts.next()?.parse().ok()?;
    let revision = parts.next()?.parse::<i32>().ok()?;
    (revision > 0 && parts.next().is_none()).then(|| (format!("edit:{id}:{revision}"), close))
}

async fn open(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    locale: &str,
    timezone: &str,
    action: &str,
    close: bool,
) -> Result<(), TelegramDiaryCommitError> {
    let binding = ConversationBinding {
        owner_id: envelope.owner_id(),
        conversation_id: envelope.conversation_id(),
    };
    let form =
        match Database::tracker_generated_form_in(txn, envelope.owner_id(), action, locale).await {
            Ok(form) => form,
            Err(
                crate::diary::actions::TrackerActionError::Missing
                | crate::diary::actions::TrackerActionError::InactiveContract
                | crate::diary::actions::TrackerActionError::EntryChanged,
            ) => {
                write_reply(
                txn,
                lease,
                envelope,
                "Этот трекер изменён или удалён. Откройте актуальное меню и выберите его снова.",
                None,
            )
            .await?;
                return Ok(());
            }
            Err(_) => return Err(TelegramDiaryCommitError::Database),
        };
    let version = version(&form)?;
    let editing = action.starts_with("edit:");
    if !allowed(txn, envelope, version, editing).await? {
        write_reply(
            txn,
            lease,
            envelope,
            if editing {
                "Доступ к этой записи изменился. Откройте /history заново."
            } else {
                "Этот трекер больше недоступен в Telegram. Откройте актуальное меню."
            },
            None,
        )
        .await?;
        return Ok(());
    }
    let timezone = owner_timezone(txn, timezone)
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?;
    let start = if close {
        begin_close(&form, locale)
    } else {
        begin_chat_form(&form, &timezone, locale)
    };
    let start = match start {
        Ok(start) => start,
        Err(ChatFormError::Answer(reply)) => {
            cancel_in(txn, binding)
                .await
                .map_err(TelegramDiaryCommitError::Clarification)?;
            write_reply(txn, lease, envelope, &reply, Some((version, editing))).await?;
            return Ok(());
        }
        Err(_) => return Err(TelegramDiaryCommitError::UnsupportedMessage),
    };
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let mut pending = json!({"text":format!("Заполнить трекер «{}»",form["title"].as_str().ok_or(TelegramDiaryCommitError::Database)?),"question":start.reply,
        "event_ids":[],"action":"log","button":"tracker_form","definition_version_id":version.to_string(),
        "channel_instance_id":format!("telegram:{}",envelope.channel().instance_id()),"created_at":OffsetTimestamp(now).to_string(),
        "prompt_advanced_at":OffsetTimestamp(now).to_string(),"prompt_order":order(txn,lease,envelope).await?});
    if editing {
        let id = action
            .split(':')
            .nth(1)
            .ok_or(TelegramDiaryCommitError::Database)?;
        let fields = pending
            .as_object_mut()
            .ok_or(TelegramDiaryCommitError::Database)?;
        fields.remove("text");
        fields.remove("question");
        pending["action"] = json!("update");
        pending["event_ids"] = json!([id]);
    }
    pending[if close { "chat_close" } else { "chat_form" }] = start.state;
    record_history_selection(txn, binding, pending)
        .await
        .map_err(TelegramDiaryCommitError::Clarification)?;
    let reply = with_safety(&start.reply, locale);
    write_reply(txn, lease, envelope, &reply, Some((version, editing))).await
}

fn version(form: &Value) -> Result<Uuid, TelegramDiaryCommitError> {
    form["action"]["definition_version_id"]
        .as_str()
        .and_then(|id| id.parse().ok())
        .ok_or(TelegramDiaryCommitError::Database)
}

async fn allowed(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
    version: Uuid,
    editing: bool,
) -> Result<bool, TelegramDiaryCommitError> {
    let destination = format!("telegram:{}", envelope.channel().instance_id());
    let destination = ShareDestination::new(DestinationKind::Channel, &destination)
        .map_err(|_| TelegramDiaryCommitError::Database)?;
    let categories = if editing {
        &[ShareCategory::Schema, ShareCategory::Facts][..]
    } else {
        &[ShareCategory::Schema][..]
    };
    share_policy::allows(
        txn,
        ShareRequirement {
            version_id: version,
            categories,
        },
        destination,
    )
    .await
    .map_err(|_| TelegramDiaryCommitError::Database)
}

async fn order(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
) -> Result<Value, TelegramDiaryCommitError> {
    let epoch: i64 = sqlx::query_scalar(
        "SELECT coalesce((payload->>'ordering_epoch')::bigint,0) FROM jobs WHERE id=$1",
    )
    .bind(lease.job_identity().0)
    .fetch_one(&mut **txn)
    .await?;
    let update = envelope
        .external_event_id()
        .parse::<i64>()
        .map_err(|_| TelegramDiaryCommitError::Database)?;
    Ok(json!([epoch, update]))
}

async fn write_reply(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    text: &str,
    shared: Option<(Uuid, bool)>,
) -> Result<(), TelegramDiaryCommitError> {
    let mut refs = Vec::new();
    if let Some((version, editing)) = shared {
        let definition: Uuid =
            sqlx::query_scalar("SELECT definition_id FROM event_definition_versions WHERE id=$1")
                .bind(version)
                .fetch_one(&mut **txn)
                .await?;
        refs.push(format!("definition:{definition}"));
        refs.push(if editing {
            format!("definition-version:{version}")
        } else {
            format!("definition-schema-version:{version}")
        });
    }
    let intent=OutboundIntent::normalize(json!({"owner_id":envelope.owner_id().to_string(),"conversation_id":envelope.conversation_id().to_string(),
        "channel_instance":{"channel":"telegram","instance_id":envelope.channel().instance_id()},"blocks":[{"text":text}],"evidence_refs":refs})).map_err(|_|TelegramDiaryCommitError::Reply)?;
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let key = format!("inbound:{}:tracker-form", lease.message_id());
    queue_outbox_intent(
        txn,
        QueueIntent {
            intent,
            operation_id: lease.operation_id(),
            inbound_id: Some(lease.message_id()),
            dedup_key: Some(&key),
            requirements: &[],
            now: StoredInstant::try_from(now).map_err(|_| TelegramDiaryCommitError::Database)?,
        },
    )
    .await
    .map_err(TelegramDiaryCommitError::Outbox)?;
    complete(txn, lease, now, TelegramInboundState::Processed).await?;
    Ok(())
}

fn say<'a>(locale: &str, ru: &'a str, en: &'a str) -> &'a str {
    if locale
        .split('-')
        .next()
        .is_some_and(|code| code.eq_ignore_ascii_case("ru"))
    {
        ru
    } else {
        en
    }
}
fn changed(locale: &str, editing: bool) -> &str {
    if editing {
        say(
            locale,
            "Запись изменилась. Откройте /history снова.",
            "Entry changed. Open /history again.",
        )
    } else {
        say(
            locale,
            "Трекер изменился. Откройте актуальное меню.",
            "Tracker changed. Open the current menu.",
        )
    }
}
fn localized_notice(notice: &str, locale: &str) -> String {
    if locale
        .split('-')
        .next()
        .is_some_and(|code| code.eq_ignore_ascii_case("ru"))
    {
        return match notice {
            "Finish tracker setup or use /cancel before opening another form." => {
                "Сначала завершите настройку трекера или отправьте /cancel.".into()
            }
            _ => notice.into(),
        };
    }
    match notice {
        "Сообщение отправлено до текущего выбора. Откройте актуальное меню." => {
            "This message predates the current prompt. Open the current menu."
        }
        "Сообщение отправлено до открытия текущего черновика. Откройте актуальное меню." => {
            "This message predates the current draft. Open the current menu."
        }
        "Сначала завершите настройку трекера или отправьте /cancel." => {
            "Finish tracker setup or use /cancel before opening another form."
        }
        other => other,
    }
    .into()
}

fn begin_close(
    form: &Value,
    locale: &str,
) -> Result<ga_domain::event_schema::chat_form::ChatFormStart, ChatFormError> {
    if form["action"]["kind"] != "edit_entry"
        || form["topology"] != "open_interval"
        || !form["initial_end"].is_null()
    {
        return Err(ChatFormError::Invalid);
    }
    Ok(ga_domain::event_schema::chat_form::ChatFormStart {
        state: json!({"action_id":form["id"],"schema_hash":form["schema_hash"],"locale":locale}),
        reply: say(
            locale,
            "Когда завершилась запись? Ответьте «сейчас» или укажите YYYY-MM-DD HH:MM.",
            "When did the entry end? Reply 'now' or enter YYYY-MM-DD HH:MM.",
        )
        .into(),
    })
}

fn advance_close(
    form: &Value,
    state: &Value,
    text: &str,
    now: OffsetDateTime,
) -> Result<ChatFormProgress, ChatFormError> {
    let locale = state["locale"].as_str().ok_or(ChatFormError::Invalid)?;
    if form["schema_hash"] != state["schema_hash"] || !form["initial_end"].is_null() {
        return Ok(ChatFormProgress::Cancelled(changed(locale, true).into()));
    }
    let parse = || {
        let end = ga_domain::event_schema::chat_form::parse_chat_form_time(
            text.trim_matches(python_whitespace),
            form["initial_timezone"]
                .as_str()
                .ok_or(ChatFormError::Invalid)?,
            now,
            locale,
        )?;
        let start = StoredInstant::legacy_input_with_offset(&form["initial_start"])
            .ok_or(ChatFormError::Invalid)?;
        if end <= start {
            return Err(ChatFormError::Answer(
                say(
                    locale,
                    "Окончание должно быть позже начала",
                    "End must be after start",
                )
                .into(),
            ));
        }
        Ok(end)
    };
    let end = match parse() {
        Ok(end) => end,
        Err(error) => {
            let detail = match error {
                ChatFormError::Answer(detail) => detail,
                ChatFormError::Invalid | ChatFormError::DecimalRange => {
                    say(locale, "Некорректное время", "Invalid time").into()
                }
            };
            let start = begin_close(form, locale)?;
            return Ok(ChatFormProgress::Continue {
                state: start.state,
                reply: format!("{detail}. {}", start.reply),
            });
        }
    };
    Ok(ChatFormProgress::Submit {
        state: state.clone(),
        submission: json!({"action_id":form["id"],"schema_hash":form["schema_hash"],"start":form["initial_start"],"end":OffsetTimestamp(end).to_string(),"timezone":form["initial_timezone"],"values":form["initial_values"],"units":form["initial_units"]}),
    })
}

fn with_safety(reply: &str, locale: &str) -> String {
    let safety = say(
        locale,
        "Форма не оценивает срочность симптомов. При внезапных тяжёлых симптомах звоните 112 или в местную экстренную службу.",
        "This form does not assess symptom urgency. For sudden severe symptoms, call 112 or your local emergency service.",
    );
    format!("{reply}\n\n{safety}")
}
