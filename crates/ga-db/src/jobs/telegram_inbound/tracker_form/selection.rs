use ga_domain::tracker_selection::{TrackerSelection, is_tracker_proposal};

use super::*;

pub(super) async fn apply_in(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    timezone: &str,
    locale: &str,
    now: StoredInstant,
    pending: Option<Value>,
) -> Result<bool, TelegramDiaryCommitError> {
    if !matches!(
        envelope.kind(),
        InboundKind::Text | InboundKind::Edit | InboundKind::Voice
    ) {
        return Ok(false);
    }
    let text = envelope.text().unwrap_or("");
    let selecting = pending
        .as_ref()
        .is_some_and(|value| value["button"] == "tracker_select");
    if text.trim_matches(python_whitespace).starts_with('/') {
        let command = text
            .trim_matches(python_whitespace)
            .split(python_whitespace)
            .next()
            .unwrap_or("");
        if selecting && !matches!(command, "/cancel" | "/undo" | "/newtracker" | "/history") {
            write_reply(
                txn,
                lease,
                envelope,
                "Неизвестная команда. Доступные команды: /help.",
                None,
            )
            .await?;
            return Ok(true);
        }
        return Ok(false);
    }
    if pending.is_some() && !selecting {
        return Ok(false);
    }
    let selection = if selecting {
        None
    } else {
        if lease.has_voice_text() {
            let original = InboundEnvelope::parse(lease.envelope_json().as_bytes())
                .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
            if original
                .text()
                .is_some_and(|caption| !caption.trim_matches(python_whitespace).is_empty())
            {
                return Ok(false);
            }
        }
        if is_tracker_proposal(text).map_err(|_| TelegramDiaryCommitError::Database)? {
            return Ok(false);
        }
        let Some(selection) =
            TrackerSelection::new(text).map_err(|_| TelegramDiaryCommitError::Database)?
        else {
            return Ok(false);
        };
        Some(selection)
    };
    let binding = ConversationBinding {
        owner_id: envelope.owner_id(),
        conversation_id: envelope.conversation_id(),
    };
    let inspected = prompt::inspect(txn, binding, lease, envelope, now).await?;
    if let Some(notice) = inspected.notice {
        write_reply(
            txn,
            lease,
            envelope,
            &localized_notice(notice, locale),
            None,
        )
        .await?;
        return Ok(true);
    }
    if inspected.draft.is_some()
        || super::super::analysis_reply::is_analytic_reply(txn, envelope).await?
    {
        return Ok(false);
    }
    if let Some(mut pending) = pending {
        let options = pending["options"].as_array().cloned().unwrap_or_default();
        let choice = text.trim_matches(python_whitespace);
        let choice = (!choice.is_empty() && choice.bytes().all(|byte| byte.is_ascii_digit()))
            .then(|| choice.parse::<usize>().ok())
            .flatten()
            .filter(|&index| index >= 1 && index <= options.len());
        if let Some(index) = choice {
            cancel_in(txn, binding)
                .await
                .map_err(TelegramDiaryCommitError::Clarification)?;
            let action = &options[index - 1];
            if current(txn, envelope, action).await? {
                open(
                    txn,
                    lease,
                    envelope,
                    locale,
                    timezone,
                    action["id"]
                        .as_str()
                        .ok_or(TelegramDiaryCommitError::Database)?,
                    false,
                )
                .await?;
            } else {
                list_changed(txn, lease, envelope, locale).await?;
            }
        } else {
            let mut shared = Vec::with_capacity(options.len());
            for action in &options {
                if !current(txn, envelope, action).await? {
                    cancel_in(txn, binding)
                        .await
                        .map_err(TelegramDiaryCommitError::Clarification)?;
                    list_changed(txn, lease, envelope, locale).await?;
                    return Ok(true);
                }
                shared.push((option_version(action)?, false));
            }
            let reply = pending["question"]
                .as_str()
                .ok_or(TelegramDiaryCommitError::Database)?
                .to_owned();
            refresh(txn, lease, envelope, &mut pending).await?;
            record_history_selection(txn, binding, pending)
                .await
                .map_err(TelegramDiaryCommitError::Clarification)?;
            let reply = if text.trim_matches(python_whitespace).is_empty() {
                "Пришлите текст или голосовое сообщение."
            } else {
                &reply
            };
            write_shared_reply(txn, lease, envelope, reply, &shared).await?;
        }
        return Ok(true);
    }
    let destination = format!("telegram:{}", envelope.channel().instance_id());
    let actions = Database::select_tracker_actions_in(
        txn,
        envelope.owner_id(),
        locale,
        &destination,
        selection
            .as_ref()
            .ok_or(TelegramDiaryCommitError::Database)?,
    )
    .await
    .map_err(|_| TelegramDiaryCommitError::Database)?;
    if actions.is_empty() {
        return Ok(false);
    }
    if let [action] = actions.as_slice() {
        open(
            txn,
            lease,
            envelope,
            locale,
            timezone,
            action["id"]
                .as_str()
                .ok_or(TelegramDiaryCommitError::Database)?,
            false,
        )
        .await?;
    } else {
        let labels: Vec<_> = actions
            .iter()
            .map(|action| safe_label(action["label"].as_str().unwrap_or("")))
            .collect();
        let items = actions
            .iter()
            .zip(&labels)
            .enumerate()
            .map(|(index, (action, label))| {
                let suffix = if labels.iter().filter(|other| *other == label).count() > 1 {
                    format!(" ({})", action["definition_key"].as_str().unwrap_or(""))
                } else {
                    String::new()
                };
                format!("{}. {label}{suffix}", index + 1)
            })
            .collect::<Vec<_>>()
            .join("; ");
        let reply = format!(
            "{}{items}",
            say(locale, "Выберите трекер: ", "Choose a tracker: ")
        );
        let mut shared = Vec::with_capacity(actions.len());
        for action in &actions {
            shared.push((option_version(action)?, false));
        }
        let mut pending = json!({"button":"tracker_select","question":reply,"options":actions,"channel_instance_id":destination});
        refresh(txn, lease, envelope, &mut pending).await?;
        record_history_selection(txn, binding, pending)
            .await
            .map_err(TelegramDiaryCommitError::Clarification)?;
        write_shared_reply(txn, lease, envelope, &reply, &shared).await?;
    }
    Ok(true)
}

fn option_version(action: &Value) -> Result<Uuid, TelegramDiaryCommitError> {
    action["definition_version_id"]
        .as_str()
        .and_then(|id| id.parse().ok())
        .ok_or(TelegramDiaryCommitError::Database)
}

async fn current(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
    action: &Value,
) -> Result<bool, TelegramDiaryCommitError> {
    Ok(
        Database::tracker_create_action_active_in(txn, envelope.owner_id(), action).await?
            && allowed(txn, envelope, option_version(action)?, false).await?,
    )
}

async fn list_changed(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    locale: &str,
) -> Result<(), TelegramDiaryCommitError> {
    write_reply(
        txn,
        lease,
        envelope,
        say(
            locale,
            "Список трекеров изменился. Откройте актуальное меню.",
            "The tracker list changed. Open the current menu.",
        ),
        None,
    )
    .await
}

async fn refresh(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    pending: &mut Value,
) -> Result<(), TelegramDiaryCommitError> {
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    pending["created_at"] = json!(OffsetTimestamp(at).to_string());
    pending["prompt_advanced_at"] = json!(OffsetTimestamp(at).to_string());
    pending["prompt_order"] = order(txn, lease, envelope).await?;
    Ok(())
}

fn safe_label(label: &str) -> String {
    let flattened: String = label
        .chars()
        .map(|ch| {
            if ch <= '\u{1f}' || ('\u{7f}'..='\u{9f}').contains(&ch) {
                ' '
            } else {
                ch
            }
        })
        .collect();
    let flattened = flattened
        .split(python_whitespace)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    let mut escaped = String::new();
    for ch in flattened.chars() {
        if "\\`*_{}[]()#+.!<>|~&-".contains(ch) {
            escaped.push('\\');
        }
        escaped.push(ch);
    }
    escaped.trim_matches(python_whitespace).to_owned()
}
