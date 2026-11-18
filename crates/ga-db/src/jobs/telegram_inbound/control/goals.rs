use ga_domain::{
    channel::InboundEnvelope, telegram_control::COMMAND_WHITESPACE, timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use super::TelegramControlError;
use crate::{
    outbox_claims::control_reply_fence,
    personal_goals::{self, GoalCommandOrder, GoalSelection, PersonalGoal, PersonalGoalsError},
    replay::REPLAY_MUTATION_LOCK,
};

const INVALID: &str = "Не удалось выбрать цели. Используйте: /goals сон самочувствие бег мигрень. Можно выбрать часть списка; /goals нет — убрать все цели.";

pub(super) async fn apply(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
    ordering_epoch: i64,
) -> Result<(String, Option<String>), TelegramControlError> {
    let text = envelope.text().ok_or(TelegramControlError::Malformed)?;
    let normalized = text.to_lowercase().replace(',', " ");
    let inputs: Vec<_> = normalized
        .split(|character| COMMAND_WHITESPACE.contains(character))
        .filter(|word| !word.is_empty())
        .skip(1)
        .take(5)
        .collect();
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(REPLAY_MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked || (inputs.is_empty() && earlier_change(txn, envelope, ordering_epoch).await?) {
        return Err(TelegramControlError::Busy);
    }
    let owner = envelope.owner_id();
    let (mut current, _) = personal_goals::load(txn, owner)
        .await
        .map_err(TelegramControlError::Goals)?;
    if !inputs.is_empty() {
        let choices = if inputs == ["нет"] {
            Some(Vec::new())
        } else {
            inputs
                .into_iter()
                .map(parse_goal)
                .collect::<Option<Vec<_>>>()
        };
        let Some(choices) = choices else {
            return Ok((INVALID.into(), None));
        };
        let clock: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut **txn)
            .await?;
        let now = StoredInstant::try_from(clock).map_err(|_| TelegramControlError::Database)?;
        let received_at = StoredInstant::try_from(envelope.received_at())
            .map_err(|_| TelegramControlError::Malformed)?;
        let source_epoch = envelope
            .occurred_at()
            .unwrap_or_else(|| envelope.received_at())
            .unix_timestamp();
        current = match personal_goals::set_in(
            txn,
            owner,
            GoalSelection {
                expected_revision: current.revision,
                goals: &choices,
                now,
                command: Some(GoalCommandOrder {
                    source_epoch,
                    received_at,
                    tie_breaker: envelope.external_event_id(),
                }),
            },
        )
        .await
        {
            Ok(current) => current,
            Err(PersonalGoalsError::InvalidSelection) => return Ok((INVALID.into(), None)),
            Err(error) => return Err(TelegramControlError::Goals(error)),
        };
    }
    let mut response = if !current.configured {
        "Цели пока не выбраны.".to_owned()
    } else if current.goals.is_empty() {
        "Личные цели отключены.".to_owned()
    } else {
        let labels: Vec<_> = current.goals.iter().copied().map(label).collect();
        format!("Ваши цели: {}.", labels.join(", "))
    };
    if current.configured && !current.goals.contains(&PersonalGoal::Running) {
        response.push_str(" Спортивная цель выключена.");
    }
    response.push_str(
        "\nВыберите нужные: /goals сон самочувствие бег мигрень. Убрать все: /goals нет.",
    );
    let fence = control_reply_fence(
        txn,
        envelope.conversation_id(),
        json!({"telegram_goals":current.revision}),
    )
    .await?;
    Ok((response, Some(fence)))
}

fn parse_goal(text: &str) -> Option<PersonalGoal> {
    Some(match text {
        "сон" => PersonalGoal::Sleep,
        "самочувствие" => PersonalGoal::Wellbeing,
        "бег" => PersonalGoal::Running,
        "мигрень" => PersonalGoal::Migraine,
        _ => return None,
    })
}

fn label(goal: PersonalGoal) -> &'static str {
    match goal {
        PersonalGoal::Sleep => "сон",
        PersonalGoal::Wellbeing => "самочувствие",
        PersonalGoal::Running => "бег",
        PersonalGoal::Migraine => "мигрень",
    }
}

async fn earlier_change(
    txn: &mut Transaction<'_, Postgres>,
    envelope: &InboundEnvelope,
    epoch: i64,
) -> Result<bool, TelegramControlError> {
    let update: i64 = envelope
        .external_event_id()
        .parse()
        .map_err(|_| TelegramControlError::Malformed)?;
    let pattern = "^ */goals +[^ ]";
    Ok(sqlx::query_scalar(
        "SELECT EXISTS(
             SELECT FROM jobs j JOIN inbound_messages i
                 ON j.dedup_key=concat('telegram:inbound:native:',i.id)
                 AND j.payload->>'inbound_message_id'=i.id::text
             WHERE j.kind='telegram_native_inbound' AND j.status IN ('pending','running')
               AND j.payload->>'lane'='control'
               AND i.owner_id=$1 AND i.channel='telegram' AND i.channel_instance_id=$2
               AND i.status='pending' AND i.legacy_telegram_update_id IS NULL
               AND (coalesce((j.payload->>'ordering_epoch')::bigint,0),i.external_event_id::bigint)<($3,$4)
               AND translate(i.envelope->>'text',$5,repeat(' ',char_length($5))) ~ $6
         ) OR EXISTS(
             SELECT FROM jobs j JOIN telegram_updates t ON j.dedup_key=concat('telegram:',t.id)
             WHERE j.kind IN ('telegram_control','telegram_update') AND j.status IN ('pending','running')
               AND t.status='pending'
               AND coalesce(jsonb_typeof(t.payload#>'{message,voice}'),'null') <> 'object'
               AND coalesce(j.payload->>'channel_instance_id','telegram:primary')=concat('telegram:',$2)
               AND (coalesce((t.payload->>'_ordering_epoch')::bigint,0),t.id)<($3,$4)
               AND (translate(t.payload#>>'{message,text}',$5,repeat(' ',char_length($5))) ~ $6
                 OR translate(t.payload#>>'{message,caption}',$5,repeat(' ',char_length($5))) ~ $6)
         )",
    )
    .bind(envelope.owner_id())
    .bind(envelope.channel().instance_id())
    .bind(epoch)
    .bind(update)
    .bind(COMMAND_WHITESPACE)
    .bind(pattern)
    .fetch_one(&mut **txn)
    .await?)
}
