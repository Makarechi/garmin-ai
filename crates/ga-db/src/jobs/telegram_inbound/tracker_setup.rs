use std::time::Duration;

use ga_domain::{
    channel::{InboundEnvelope, InboundKind},
    event_payload::python_whitespace,
    event_schema::{
        CustomDefinitionError, CustomSchemaError, TrackerSetupDraft, TrackerSetupError,
        parse_tracker_chat_field,
    },
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    TelegramDiaryCommitError, TelegramInboundLease,
    history::prompt,
    local_text::{preferred_locale, verify_order, write_reply},
};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{ManageCustomDefinition, actions::TrackerSetupStoreError},
    health_analysis::owner_timezone,
    outbox_enqueue::QueueIntentError,
};

struct SetupClock {
    now: OffsetDateTime,
    message: OffsetDateTime,
    received: OffsetDateTime,
    update: i64,
    epoch: i64,
    storage: i64,
}

impl Database {
    /// Advance the local tracker draft and its durable reply in the same inbox transaction.
    pub async fn apply_telegram_tracker_setup(
        &self,
        lease: &TelegramInboundLease,
        configured_timezone: &str,
        configured_locale: &str,
    ) -> Result<(), TelegramDiaryCommitError> {
        let envelope = lease
            .processing_envelope()
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        if envelope.channel().channel() != "telegram"
            || envelope.message_id() != lease.message_id()
            || !matches!(
                envelope.kind(),
                InboundKind::Text | InboundKind::Edit | InboundKind::Voice
            )
        {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        tokio::time::timeout(
            Duration::from_millis(2500),
            self.tracker_setup_inbox(lease, &envelope, configured_timezone, configured_locale),
        )
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }

    async fn tracker_setup_inbox(
        &self,
        lease: &TelegramInboundLease,
        envelope: &InboundEnvelope,
        configured_timezone: &str,
        configured_locale: &str,
    ) -> Result<(), TelegramDiaryCommitError> {
        let mut txn = self.begin_telegram_text(lease, envelope).await?;
        verify_order(&mut txn, lease).await?;
        if let Some(reply) = reply_in(
            &mut txn,
            lease,
            envelope,
            configured_timezone,
            configured_locale,
        )
        .await?
        {
            let now = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            write_reply(&mut txn, lease, envelope, now, &reply).await?;
            txn.commit().await?;
            Ok(())
        } else {
            txn.commit().await?;
            Err(TelegramDiaryCommitError::Outbox(QueueIntentError::Busy))
        }
    }
}

pub(super) async fn reply_in(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    configured_timezone: &str,
    configured_locale: &str,
) -> Result<Option<String>, TelegramDiaryCommitError> {
    verify_order(txn, lease).await?;
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let epoch: i64 = sqlx::query_scalar(
        "SELECT coalesce((payload->>'ordering_epoch')::bigint,0) FROM jobs WHERE id=$1",
    )
    .bind(lease.job_identity().0)
    .fetch_one(&mut **txn)
    .await?;
    let update: i64 = envelope
        .external_event_id()
        .parse()
        .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
    let clock = SetupClock {
        now,
        message: envelope.occurred_at().unwrap_or(envelope.received_at()),
        received: envelope.received_at(),
        update,
        epoch,
        storage: legacy_storage_counter(txn, envelope, update).await?,
    };
    let key = format!(
        "tracker:chat-setup:telegram:{}",
        envelope.channel().instance_id()
    );
    let row: Option<(String, OffsetDateTime)> =
        sqlx::query_as("SELECT value::text,updated_at FROM app_state WHERE key=$1 FOR UPDATE")
            .bind(&key)
            .fetch_optional(&mut **txn)
            .await?;
    let mut state = row
        .map(|(raw, updated)| {
            serde_json::from_str::<Value>(&raw)
                .map(|state| (state, updated))
                .map_err(|_| TelegramDiaryCommitError::Database)
        })
        .transpose()?;
    let locale = preferred_locale(
        txn,
        ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        },
        configured_locale,
    )
    .await?;
    let answer = envelope
        .text()
        .unwrap_or("")
        .trim_matches(python_whitespace);
    let predates = state
        .as_ref()
        .is_some_and(|(state, _)| predates_start(state, &clock));
    if predates && answer.split(python_whitespace).next() != Some("/newtracker") {
        let current = state
            .as_ref()
            .is_some_and(|(state, updated)| active_at(state, *updated, clock.now));
        if !current {
            return Ok(None);
        }
    }
    let response = if predates {
        say(
            &locale,
            "Сообщение отправлено до открытия текущего черновика. Откройте актуальное меню.",
            "This message predates the current draft. Open the current menu.",
        )
        .to_owned()
    } else {
        if state.as_ref().is_some_and(|(state, updated)| {
            let activity = match &state["last_activity_at"] {
                Value::Null => Some(*updated),
                Value::String(value) if value.is_empty() => Some(*updated),
                value => stamp(value),
            };
            activity.is_none_or(|activity| activity < clock.message - time::Duration::hours(24))
        }) {
            sqlx::query("DELETE FROM app_state WHERE key=$1")
                .bind(&key)
                .execute(&mut **txn)
                .await?;
            state = None;
        }
        if answer.split(python_whitespace).next() == Some("/newtracker") {
            if let Some((mut state, _)) = state {
                state["last_activity_at"] = encoded(clock.now)?;
                save(txn, &key, &state).await?;
                say(
                    &locale,
                    "Черновик уже открыт. Пришлите ответ, /preview или /cancel.",
                    "A draft is already open. Reply, use /preview or /cancel.",
                )
                .to_owned()
            } else {
                let current = prompt::inspect(
                    txn,
                    ConversationBinding {
                        owner_id: envelope.owner_id(),
                        conversation_id: envelope.conversation_id(),
                    },
                    lease,
                    envelope,
                    instant(clock.now)?,
                )
                .await?;
                if current.pending {
                    say(
                        &locale,
                        "Сначала завершите текущую форму или отправьте /cancel.",
                        "Finish the current form or use /cancel before creating a tracker.",
                    )
                    .to_owned()
                } else {
                    let timezone = owner_timezone(txn, configured_timezone)
                        .await
                        .map_err(|_| TelegramDiaryCommitError::Database)?;
                    let advanced: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                        .fetch_one(&mut **txn)
                        .await?;
                    let state = json!({"key":format!("chat_{}",&Uuid::new_v4().simple().to_string()[..16]),
                                "name":null,"fields":[],"locale":locale,"timezone":timezone,"privacy":"private",
                                "confirmation_token":null,"started_at":encoded(clock.message)?,
                                "started_update_id":clock.storage,"started_provider_update_id":clock.update,
                                "started_ordering_epoch":clock.epoch,"last_prompt_advanced_at":encoded(advanced)?,
                                "last_provider_update_id":clock.update,"last_ordering_epoch":clock.epoch,
                                "last_update_id":clock.storage,"last_activity_at":encoded(clock.now)?});
                    save(txn, &key, &state).await?;
                    say(
                        &locale,
                        "Как назвать новый трекер?",
                        "What should the new tracker be called?",
                    )
                    .to_owned()
                }
            }
        } else if let Some((state, _)) = state {
            advance(txn, &key, state, answer, &clock, envelope, &locale).await?
        } else {
            return Ok(None);
        }
    };
    Ok(Some(response))
}

async fn legacy_storage_counter(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
    update: i64,
) -> Result<i64, TelegramDiaryCommitError> {
    let existing: Option<String> =
        sqlx::query_scalar("SELECT payload::text FROM telegram_updates WHERE id=$1")
            .bind(update)
            .fetch_optional(&mut **txn)
            .await?;
    let instance = json!({"channel":"telegram","instance_id":envelope.channel().instance_id()});
    let primary = json!({"channel":"telegram","instance_id":"primary"});
    let existing = existing
        .map(|raw| serde_json::from_str::<Value>(&raw))
        .transpose()
        .map_err(|_| TelegramDiaryCommitError::Database)?;
    if existing
        .is_none_or(|existing| existing.get("_channel_instance").unwrap_or(&primary) == &instance)
    {
        return Ok(update);
    }
    let namespace = format!("telegram:{}:{update}", envelope.channel().instance_id());
    let digest = ga_domain::sha256_hex(namespace.as_bytes());
    let number =
        u64::from_str_radix(&digest[..16], 16).map_err(|_| TelegramDiaryCommitError::Database)?;
    let storage = -((number & i64::MAX as u64).max(1) as i64);
    let collision: Option<String> =
        sqlx::query_scalar("SELECT payload::text FROM telegram_updates WHERE id=$1")
            .bind(storage)
            .fetch_optional(&mut **txn)
            .await?;
    if let Some(raw) = collision {
        let record: Value =
            serde_json::from_str(&raw).map_err(|_| TelegramDiaryCommitError::Database)?;
        let provider = record["update_id"]
            .as_i64()
            .or_else(|| record["update_id"].as_bool().map(i64::from));
        if provider != Some(update) || record["_channel_instance"] != instance {
            return Err(TelegramDiaryCommitError::Database);
        }
    }
    Ok(storage)
}

fn active_at(state: &Value, updated: OffsetDateTime, at: OffsetDateTime) -> bool {
    let activity = match &state["last_activity_at"] {
        Value::Null => Some(updated),
        Value::String(value) if value.is_empty() => Some(updated),
        value => stamp(value),
    };
    stamp(&state["started_at"]).is_none_or(|started| started <= at)
        && activity.is_some_and(|activity| activity >= at - time::Duration::hours(24))
}

fn predates_start(state: &Value, clock: &SetupClock) -> bool {
    let earlier = state["started_provider_update_id"].as_i64().map_or_else(
        || {
            state["started_update_id"]
                .as_i64()
                .is_some_and(|update| clock.storage < update)
        },
        |update| {
            (clock.epoch, clock.update)
                < (
                    state["started_ordering_epoch"].as_i64().unwrap_or(0),
                    update,
                )
        },
    );
    earlier || stamp(&state["started_at"]).is_some_and(|started| started > clock.message)
}

async fn advance(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    mut state: Value,
    answer: &str,
    clock: &SetupClock,
    envelope: &InboundEnvelope,
    configured_locale: &str,
) -> Result<String, TelegramDiaryCommitError> {
    let locale = state["locale"]
        .as_str()
        .ok_or(TelegramDiaryCommitError::Database)?
        .to_owned();
    if answer.is_empty() && envelope.kind() == InboundKind::Voice {
        return Ok(say(configured_locale,"Не удалось обработать голос. Напишите ответ текстом или добавьте подпись к голосовому сообщению.",
            "Voice is unavailable. Type your answer or add a caption to the voice message.").to_owned());
    }
    let order = state
        .get("last_provider_update_id")
        .unwrap_or(&state["started_provider_update_id"])
        .as_i64()
        .map(|update| {
            (
                state["last_ordering_epoch"]
                    .as_i64()
                    .or_else(|| state["started_ordering_epoch"].as_i64())
                    .unwrap_or(0),
                update,
            )
        });
    let stale = order.map_or_else(
        || {
            state
                .get("last_update_id")
                .unwrap_or(&state["started_update_id"])
                .as_i64()
                .is_some_and(|last| clock.storage <= last)
        },
        |last| (clock.epoch, clock.update) <= last,
    );
    if stale
        || answer != "/cancel"
            && stamp(&state["last_prompt_advanced_at"]).is_some_and(|at| clock.received <= at)
    {
        return Ok(if answer == "/confirm_tracker"
            && state["confirmation_token"].is_string()
            && !stale
        {
            say(
                &locale,
                "Подтверждение отправлено до предпросмотра. Откройте /preview снова.",
                "Confirmation predates the preview. Use /preview again.",
            )
        } else {
            say(
                &locale,
                "Сообщение отправлено до текущего шага. Откройте актуальное меню.",
                "This message predates the current step. Open the current menu.",
            )
        }
        .to_owned());
    }
    if answer == "/cancel" {
        sqlx::query("DELETE FROM app_state WHERE key=$1")
            .bind(key)
            .execute(&mut **txn)
            .await?;
        return Ok(say(&locale, "Черновик удалён.", "Draft discarded.").to_owned());
    }
    state["last_provider_update_id"] = json!(clock.update);
    state["last_ordering_epoch"] = json!(clock.epoch);
    state["last_update_id"] = json!(clock.storage);
    state["last_prompt_advanced_at"] = encoded(clock.now)?;
    state["last_activity_at"] = encoded(clock.now)?;
    save(txn, key, &state).await?;
    let response = if let Some(privacy) = answer.strip_prefix("/privacy ") {
        let privacy = privacy.trim_matches(python_whitespace).to_lowercase();
        if !matches!(privacy.as_str(), "private" | "sensitive") {
            return Ok(say(
                &locale,
                "Используйте /privacy private или /privacy sensitive.",
                "Use /privacy private or /privacy sensitive.",
            )
            .to_owned());
        }
        state["privacy"] = json!(privacy);
        state["confirmation_token"] = Value::Null;
        say(
            &locale,
            "Приватность черновика обновлена.",
            "Draft privacy updated.",
        )
        .to_owned()
    } else if !state["name"].as_str().is_some_and(|name| !name.is_empty()) {
        if !(1..=64).contains(&answer.chars().count()) || answer.starts_with('/') {
            return Ok(say(
                &locale,
                "Название должно быть от 1 до 64 символов.",
                "Name must be 1–64 characters.",
            )
            .to_owned());
        }
        state["name"] = json!(answer);
        field_help(&locale).to_owned()
    } else {
        match answer {
            "/remove_field" => {
                let fields = state["fields"]
                    .as_array_mut()
                    .ok_or(TelegramDiaryCommitError::Database)?;
                if fields.pop().is_none() {
                    return Ok(field_help(&locale).to_owned());
                }
                state["confirmation_token"] = Value::Null;
                say(&locale, "Последнее поле удалено.", "Last field removed.").to_owned()
            }
            "/preview" => {
                if state["fields"].as_array().is_some_and(Vec::is_empty) {
                    return Ok(field_help(&locale).to_owned());
                }
                let draft = match draft(&state) {
                    Ok(draft) => draft,
                    Err(TrackerSetupError::Definition(CustomDefinitionError::Schema(
                        CustomSchemaError::Size,
                    ))) => return Ok(schema_limit(&locale).to_owned()),
                    Err(_) => return Err(TelegramDiaryCommitError::UnsupportedMessage),
                };
                let preview = Database::preview_tracker_setup_in(txn, &draft)
                    .await
                    .map_err(store_error)?;
                state["confirmation_token"] = preview["confirmation_token"].clone();
                let issued: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                    .fetch_one(&mut **txn)
                    .await?;
                state["preview_issued_at"] = encoded(issued)?;
                preview_text(&state, &locale)?
            }
            "/confirm_tracker" => {
                let Some(token) = state["confirmation_token"].as_str() else {
                    return Ok(
                        say(&locale, "Сначала откройте /preview.", "Use /preview first.")
                            .to_owned(),
                    );
                };
                if stamp(&state["preview_issued_at"]).is_some_and(|at| clock.received <= at) {
                    return Ok(say(
                        &locale,
                        "Подтверждение отправлено до предпросмотра. Откройте /preview снова.",
                        "Confirmation predates the preview. Use /preview again.",
                    )
                    .to_owned());
                }
                let draft =
                    draft(&state).map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
                let actor = format!("telegram:{}", envelope.sender_ref());
                match Database::confirm_tracker_setup_in(
                    txn,
                    ManageCustomDefinition {
                        owner_id: envelope.owner_id(),
                        authenticated_actor: &actor,
                        may_manage_definitions: true,
                    },
                    &draft,
                    token,
                )
                .await
                {
                    Ok(created) => {
                        sqlx::query("DELETE FROM app_state WHERE key=$1")
                            .bind(key)
                            .execute(&mut **txn)
                            .await?;
                        return Ok(format!(
                            "{}: {}",
                            say(&locale, "Трекер создан", "Tracker created"),
                            literal(
                                created["tracker"]["shortcut"]
                                    .as_str()
                                    .ok_or(TelegramDiaryCommitError::Database)?
                            )
                        ));
                    }
                    Err(TrackerSetupStoreError::PreviewChanged) => {
                        state["confirmation_token"] = Value::Null;
                        say(
                            &locale,
                            "Предпросмотр устарел. Откройте /preview снова.",
                            "Preview expired. Use /preview again.",
                        )
                        .to_owned()
                    }
                    Err(error) => return Err(store_error(error)),
                }
            }
            _ => {
                let fields = state["fields"]
                    .as_array_mut()
                    .ok_or(TelegramDiaryCommitError::Database)?;
                if fields.len() >= 32 {
                    return Ok(field_help(&locale).to_owned());
                }
                let field = match parse_tracker_chat_field(
                    answer,
                    &format!("field_{}", fields.len() + 1),
                ) {
                    Ok(field) => field,
                    Err(_) => return Ok(field_help(&locale).to_owned()),
                };
                fields.push(field);
                state["confirmation_token"] = Value::Null;
                match draft(&state) {
                    Ok(_) => {}
                    Err(TrackerSetupError::Definition(CustomDefinitionError::Schema(
                        CustomSchemaError::Size,
                    ))) => return Ok(schema_limit(&locale).to_owned()),
                    Err(_) => return Ok(field_help(&locale).to_owned()),
                }
                say(
                    &locale,
                    "Поле добавлено. Добавьте ещё или откройте /preview.",
                    "Field added. Add another or use /preview.",
                )
                .to_owned()
            }
        }
    };
    save(txn, key, &state).await?;
    Ok(response)
}

fn draft(state: &Value) -> Result<TrackerSetupDraft, TrackerSetupError> {
    TrackerSetupDraft::from_json(&json!({"key":state["key"],"name":state["name"],
        "locale":state["locale"],"reminder_timezone":state["timezone"],
        "privacy":state["privacy"],"fields":state["fields"]}))
}

fn preview_text(state: &Value, locale: &str) -> Result<String, TelegramDiaryCommitError> {
    let fields = state["fields"]
        .as_array()
        .ok_or(TelegramDiaryCommitError::Database)?;
    let mut lines = Vec::with_capacity(fields.len() + 2);
    lines.push(format!(
        "{}: {}",
        say(locale, "Предпросмотр", "Preview"),
        literal(
            state["name"]
                .as_str()
                .ok_or(TelegramDiaryCommitError::Database)?
        )
    ));
    for field in fields {
        let mut details = vec![
            field["kind"]
                .as_str()
                .ok_or(TelegramDiaryCommitError::Database)?
                .to_owned(),
        ];
        if !field["minimum"].is_null() && !field["maximum"].is_null() {
            details.push(format!("{}–{}", field["minimum"], field["maximum"]));
        }
        if let Some(unit) = field["unit"].as_str() {
            details.push(literal(unit));
        }
        if let Some(options) = field["options"]
            .as_array()
            .filter(|options| !options.is_empty())
        {
            details.push(
                options
                    .iter()
                    .map(|option| {
                        option
                            .as_str()
                            .map(literal)
                            .ok_or(TelegramDiaryCommitError::Database)
                    })
                    .collect::<Result<Vec<_>, _>>()?
                    .join(", "),
            );
        }
        lines.push(format!(
            "{} | {}",
            literal(
                field["label"]
                    .as_str()
                    .ok_or(TelegramDiaryCommitError::Database)?
            ),
            details.join(" ")
        ));
    }
    let privacy = state["privacy"]
        .as_str()
        .ok_or(TelegramDiaryCommitError::Database)?;
    lines.push(if russian(locale) {
        format!("Приватность: {privacy}. Изменить: /privacy private или /privacy sensitive. Подтвердите командой /confirm_tracker или продолжите редактирование.")
    } else { format!("Privacy: {privacy}. Change it with /privacy private or /privacy sensitive. Use /confirm_tracker to create it, or keep editing.") });
    if privacy == "sensitive" {
        lines.push(say(locale,
        "После создания чувствительный трекер будет недоступен в Telegram, пока вы отдельно не разрешите этому каналу доступ к схеме и фактам в настройках согласия.",
        "After creation, this sensitive tracker will be unavailable in Telegram until you separately allow this channel access to its schema and facts in consent settings.").to_owned());
    }
    Ok(lines.join("\n"))
}

fn literal(text: &str) -> String {
    let mut result = String::new();
    for character in text.chars() {
        if "\\`*_{}[]()#+.!<>|~-".contains(character) {
            result.push('\\');
        }
        result.push(character);
    }
    result
}

fn russian(locale: &str) -> bool {
    locale
        .split('-')
        .next()
        .is_some_and(|language| language.eq_ignore_ascii_case("ru"))
}
fn say<'a>(locale: &str, ru: &'a str, en: &'a str) -> &'a str {
    if russian(locale) { ru } else { en }
}
fn field_help(locale: &str) -> &'static str {
    say(
        locale,
        "Добавьте поле: «Оценка | шкала 1-5», «Количество | счётчик 0-100», «Заметка | текст», «Есть симптом | да/нет» или «Тип | выбор A, B». Выберите приватность: /privacy private или /privacy sensitive. Затем /preview. Последнее поле можно убрать через /remove_field.",
        "Add a field: 'Rating | scale 1-5', 'Count | count 0-100', 'Note | text', 'Present | yes/no' or 'Type | choice A, B'. Choose privacy with /privacy private or /privacy sensitive. Then use /preview. Use /remove_field to remove the last field.",
    )
}
fn schema_limit(locale: &str) -> &'static str {
    say(
        locale,
        "Схема трекера превышает 32 КиБ. Сократите варианты или удалите поле через /remove_field.",
        "The tracker schema exceeds 32 KiB. Shorten the choices or remove a field with /remove_field.",
    )
}
fn stamp(value: &Value) -> Option<OffsetDateTime> {
    StoredInstant::from_legacy_input(value).map(OffsetDateTime::from)
}
fn instant(at: OffsetDateTime) -> Result<StoredInstant, TelegramDiaryCommitError> {
    StoredInstant::try_from(at).map_err(|_| TelegramDiaryCommitError::Database)
}
fn encoded(at: OffsetDateTime) -> Result<Value, TelegramDiaryCommitError> {
    Ok(json!(instant(at)?.to_string()))
}
fn store_error(_: TrackerSetupStoreError) -> TelegramDiaryCommitError {
    TelegramDiaryCommitError::Database
}
async fn save(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    state: &Value,
) -> Result<(), TelegramDiaryCommitError> {
    sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=clock_timestamp()")
        .bind(key).bind(state.to_string()).execute(&mut **txn).await?;
    Ok(())
}
