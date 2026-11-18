use ga_db::{
    Database,
    diary::StoredSystemEvent,
    jobs::telegram_inbound::{
        ModelContinuation, ModelContinuationReply, TelegramDiaryCommitError, TelegramInboundLease,
    },
};
use ga_domain::{
    agent_interpretation::{AgentIntent, TimeClarification},
    channel::{InboundEnvelope, OutboundIntent},
    event_input::source_calendar_wall_time,
    localization::catalog_text,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};

use crate::{model_gateway::TelegramDiaryInterpretation, urgent_screen::urgent_notice};

pub enum DiaryReplyOutcome {
    Published,
    NeedsAnalysis,
}

pub struct DiaryReplyPreferences<'a> {
    pub configured_timezone: &'a str,
    pub owner_locale: &'a str,
    pub started: StoredInstant,
    pub undo_anchor: Option<i64>,
}

/// Apply a classified message against its original context and render only the saved result.
pub async fn publish_telegram_interpretation(
    database: &Database,
    lease: &TelegramInboundLease,
    interpretation: &TelegramDiaryInterpretation,
    preferences: DiaryReplyPreferences<'_>,
) -> Result<DiaryReplyOutcome, TelegramDiaryCommitError> {
    let command = &interpretation.command;
    let language = command
        .response_language()
        .unwrap_or(preferences.owner_locale);
    let expected = interpretation.publication_context(preferences.configured_timezone);
    let render_saved = |envelope: &InboundEnvelope, saved: &[StoredSystemEvent]| {
        text_reply(envelope, &saved_message(saved, language)?)
    };
    let intent = command.intent();
    let result = if command.confidence() < 0.85
        && !matches!(
            intent,
            AgentIntent::Question | AgentIntent::Clarify | AgentIntent::Safety
        ) {
        Err(TelegramDiaryCommitError::ClarificationRequired)
    } else {
        match intent {
            AgentIntent::Log => {
                database
                    .apply_telegram_model_log(lease, command, expected, render_saved)
                    .await
            }
            AgentIntent::Update | AgentIntent::Close => {
                database
                    .apply_telegram_model_correction(lease, command, expected, render_saved)
                    .await
            }
            AgentIntent::Undo => {
                let work = ModelContinuation::Undo {
                    offered_audit: preferences.undo_anchor,
                };
                database
                    .apply_telegram_model_continuation(lease, expected, work, |envelope, reply| {
                        text_reply(envelope, &continued_message(reply, language)?)
                    })
                    .await
            }
            AgentIntent::Acknowledge if command.events().is_empty() => {
                let question_id = command
                    .target_question_id()
                    .ok_or(TelegramDiaryCommitError::ExistingContextRequired)?;
                database
                    .apply_telegram_model_continuation(
                        lease,
                        expected,
                        ModelContinuation::Acknowledge { question_id },
                        |envelope, reply| {
                            text_reply(envelope, &continued_message(reply, language)?)
                        },
                    )
                    .await
            }
            AgentIntent::Acknowledge => Err(TelegramDiaryCommitError::ClarificationRequired),
            AgentIntent::Clarify => {
                let question = command
                    .clarification()
                    .filter(|text| !text.trim().is_empty())
                    .or_else(|| catalog_text(language, "agent.record_fallback"))
                    .ok_or(TelegramDiaryCommitError::Reply)?;
                database
                    .apply_telegram_model_clarification(
                        lease,
                        preferences.started,
                        question,
                        language,
                        expected,
                    )
                    .await
            }
            AgentIntent::Safety => {
                database
                    .finish_telegram_model_notice(lease, urgent_notice(language))
                    .await?;
                return Ok(DiaryReplyOutcome::Published);
            }
            AgentIntent::Question => return Ok(DiaryReplyOutcome::NeedsAnalysis),
        }
    };
    if let Err(error) = result {
        let key = match error {
            TelegramDiaryCommitError::TimeClarification(
                TimeClarification::MissingTargetTimezone,
            ) => "agent.record_details",
            TelegramDiaryCommitError::TimeClarification(TimeClarification::TimezoneMismatch) => {
                "agent.timezone_mismatch"
            }
            TelegramDiaryCommitError::TimeClarification(TimeClarification::AmbiguousTime) => {
                "agent.ambiguous_time"
            }
            TelegramDiaryCommitError::TimeClarification(TimeClarification::FutureTime) => {
                "agent.future_time"
            }
            TelegramDiaryCommitError::MedicationEvidenceRequired => "agent.medication_confirm",
            TelegramDiaryCommitError::ClarificationRequired => "agent.record_details",
            _ => return Err(error),
        };
        let question = catalog_text(language, key).ok_or(TelegramDiaryCommitError::Reply)?;
        database
            .apply_telegram_model_clarification(
                lease,
                preferences.started,
                question,
                language,
                expected,
            )
            .await?;
    }
    Ok(DiaryReplyOutcome::Published)
}

pub(super) fn text_reply(envelope: &InboundEnvelope, text: &str) -> Option<OutboundIntent> {
    OutboundIntent::normalize(json!({
        "owner_id":envelope.owner_id().to_string(),"conversation_id":envelope.conversation_id().to_string(),
        "channel_instance":{"channel":"telegram","instance_id":envelope.channel().instance_id()},
        "blocks":[{"text":text}],
    })).ok()
}

fn continued_message(reply: ModelContinuationReply, language: &str) -> Option<String> {
    let key = match reply {
        ModelContinuationReply::Undone(count) if count > 1 => {
            return Some(
                catalog_text(language, "agent.undo_many")?.replace("{count}", &count.to_string()),
            );
        }
        ModelContinuationReply::Undone(_) => "agent.undo_one",
        ModelContinuationReply::ContextUnknown => "agent.context_unknown",
        ModelContinuationReply::EpisodeOpen => "agent.episode_open",
        ModelContinuationReply::EpisodeChanged => "agent.episode_changed",
    };
    catalog_text(language, key).map(str::to_owned)
}

fn saved_message(saved: &[StoredSystemEvent], language: &str) -> Option<String> {
    let labels = saved
        .iter()
        .map(|event| {
            let event = &event.snapshot;
            let kind = event["kind"].as_str()?;
            let at = StoredInstant::from_legacy_input(&event["start"])?;
            let zone = event["timezone"].as_str()?;
            let (day, clock) = source_calendar_wall_time(at, zone)?;
            let label = event_label(kind, &event["payload"], language)?;
            Some(format!(
                "{label} ({:02}.{:02} {:02}:{:02})",
                day.day(),
                u8::from(day.month()),
                clock.hour(),
                clock.minute()
            ))
        })
        .collect::<Option<Vec<_>>>()?;
    Some(catalog_text(language, "agent.saved")?.replace("{records}", &labels.join(", ")))
}

fn event_label(kind: &str, fields: &Value, language: &str) -> Option<String> {
    let fallback = |key: &str| catalog_text(language, key);
    match kind {
        "headache_observation" => {
            let headache = fallback(&format!("agent.answer.{}", fields["headache"].as_str()?))?;
            let migraine = fallback(&format!("agent.answer.{}", fields["migraine"].as_str()?))?;
            Some(
                fallback("agent.headache_label")?
                    .replace("{headache}", headache)
                    .replace("{migraine}", migraine),
            )
        }
        "medication" => {
            let name = fields["name"]
                .as_str()
                .filter(|text| !text.is_empty())
                .or_else(|| fallback("agent.medication_unknown_name"))?;
            let dose = fields["dose"]
                .as_number()
                .map(|dose| dose.as_str())
                .or_else(|| fallback("agent.medication_unknown_dose"))?;
            let unit = fields["unit"]
                .as_str()
                .filter(|text| !text.is_empty())
                .or_else(|| fallback("agent.medication_unknown_unit"))?;
            Some(
                fallback("agent.medication_label")?
                    .replace("{dose}", dose)
                    .replace("{unit}", unit)
                    .replace("{name}", name),
            )
        }
        _ => Some(
            fallback(&format!("agent.saved.{kind}"))
                .unwrap_or(kind)
                .to_owned(),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    #[test]
    fn confirms_saved_time_and_labels_in_the_owner_language() {
        let entry = StoredSystemEvent {
            id: Uuid::nil(),
            revision: 1,
            reused: false,
            snapshot: json!({"kind":"note","start":"2026-03-29T01:22:00Z",
                "timezone":"Europe/Bratislava","payload":{"type":"note"}}),
        };
        let entries = [entry];
        assert_eq!(
            saved_message(&entries, "en-GB").as_deref(),
            Some("Saved: note (29.03 03:22). You can correct a record with a regular message.")
        );
        assert_eq!(
            saved_message(&entries, "ru-RU").as_deref(),
            Some("Сохранил: заметку (29.03 03:22). Исправить запись можно обычным сообщением.")
        );
        assert_eq!(
            continued_message(ModelContinuationReply::Undone(2), "en").as_deref(),
            Some("Last operation undone: 2 records.")
        );
    }
}
