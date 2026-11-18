use std::{env, error::Error, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    conversation_memory::ConversationBinding,
    diary::{
        ClarificationClock, CreateSystemEvent, InterpretationContextQuery, StoredSystemEvent,
        UpdateSystemEvent,
    },
    jobs::telegram_inbound::{
        LocalTelegramText, ModelContinuation, ModelContinuationReply, TelegramDiaryCommitError,
        TelegramInboundTarget, TelegramModelAdmission, TelegramModelContext,
        TelegramSafetyDecision,
    },
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::{
    agent_interpretation::AgentInterpretation,
    channel::{InboundEnvelope, OutboundIntent, telegram_conversation_id},
    event_input::{EventInput, ValidationMode},
    timestamp::StoredInstant,
};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

fn note(at: StoredInstant, label: &str) -> Result<EventInput, Box<dyn Error>> {
    let input = serde_json::to_vec(&json!({
        "start":at.to_string(),"timezone":"UTC","source":"manual",
        "payload":{"type":"note","description":label}
    }))?;
    Ok(EventInput::from_json(
        &input,
        32_000,
        ValidationMode::NewReport,
    )?)
}

#[tokio::test]
#[ignore = "verifies authenticated correction and stale-context rejection on isolated TimescaleDB"]
async fn model_correction_rejects_a_newer_entry_and_publishes_only_current_fields()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected fixture database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let started = StoredInstant::try_from(at)?;
    let message_at =
        StoredInstant::try_from(OffsetDateTime::from_unix_timestamp(at.unix_timestamp())?)?;
    let entry_at = StoredInstant::try_from(at - Duration::minutes(15))?;
    let entry = database
        .create_system_event(CreateSystemEvent {
            owner_id: owner,
            authenticated_actor: "synthetic-fixture",
            report: &note(entry_at, "Synthetic original note")?,
            idempotency_key: None,
            operation_id: None,
            clock_uncertainty_seconds: None,
        })
        .await?;

    let mut runtime = database.acquire_runtime_lease().await?;
    let binding = TelegramBinding {
        owner_id: owner,
        instance_id: "primary",
        external_owner_id: 42,
    };
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let receipt = binding
        .accept_polled_update(
            &mut runtime,
            &json!({
                "update_id":901,"message":{"message_id":901,"date":at.unix_timestamp(),
                    "chat":{"id":42,"type":"private"},"from":{"id":42},
                    "text":"Edit the synthetic note to corrected description"},
            }),
            started,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing owner message")?;
    assert!(
        database
            .claim_telegram_model_text(target, started, 60)
            .await?
            .is_none()
    );
    let screen = database
        .claim_telegram_local_screen(target, started, 60)
        .await?
        .ok_or("Expected unscreened text")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await?;
    let lease = database
        .claim_telegram_model_text(target, started, 60)
        .await?
        .ok_or("Expected screened text")?;
    assert!(matches!(
        database.admit_telegram_model_text(&lease, "UTC", "en").await?,
        TelegramModelAdmission::Ready { timezone, locale, .. } if timezone=="UTC" && locale=="en"
    ));

    let model = "model:gemini:primary";
    let context_query = || -> Result<InterpretationContextQuery<'_>, Box<dyn Error>> {
        Ok(InterpretationContextQuery {
            binding: ConversationBinding {
                owner_id: owner,
                conversation_id: telegram_conversation_id(owner, "primary", 42),
            },
            model: ShareDestination::new(DestinationKind::Model, model)?,
            now: message_at,
            clarification_clock: ClarificationClock {
                message_at,
                conversation_now: started,
                use_message_time: false,
            },
            mentioned_events: &[],
        })
    };
    let proof = |digest| TelegramModelContext {
        model_instance: model,
        configured_timezone: "UTC",
        timezone: "UTC",
        mentioned_events: &[],
        digest,
    };
    let confirmation = |input: &InboundEnvelope, changed: &[StoredSystemEvent]| {
        assert_eq!(changed.len(), 1);
        assert_eq!(
            changed[0].snapshot["payload"]["description"],
            "Synthetic corrected description"
        );
        OutboundIntent::normalize(json!({
            "owner_id":input.owner_id().to_string(),"conversation_id":input.conversation_id().to_string(),
            "channel_instance":{"channel":"telegram","instance_id":"primary"},
            "blocks":[{"text":"Synthetic correction confirmation"}],
        })).ok()
    };
    let command = AgentInterpretation::from_value(&json!({
        "intent":"update","confidence":0.99,"target_event_id":entry.id.to_string(),
        "changed_fields":["payload.description"],
        "events":[{"start":entry_at.to_string(),"timezone":"UTC",
          "payload":{"type":"note","description":"Synthetic corrected description"}}],
    }))?;
    let initial = database
        .interpretation_context(context_query()?)
        .await?
        .digest()?;
    let changed = database
        .update_system_event(UpdateSystemEvent {
            owner_id: owner,
            authenticated_actor: "api",
            event_id: entry.id,
            expected_revision: entry.revision,
            operation_id: None,
            report: &note(entry_at, "Synthetic intervening change")?,
        })
        .await?;
    assert_eq!(changed.revision, entry.revision + 1);
    assert!(matches!(
        database
            .apply_telegram_model_correction(&lease, &command, proof(&initial), confirmation)
            .await,
        Err(TelegramDiaryCommitError::ModelContextChanged)
    ));
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM outbox_messages")
        .fetch_one(&pool)
        .await?;
    assert_eq!(count, 0);
    let current = database
        .interpretation_context(context_query()?)
        .await?
        .digest()?;
    let queued = database
        .apply_telegram_model_correction(&lease, &command, proof(&current), confirmation)
        .await?;
    let state: (i32, String, String, bool) = sqlx::query_as(
        "SELECT e.revision,a.actor,i.status,o.memory_fence ? 'target_epoch'
         FROM events e JOIN audit_log a ON a.event_id=e.id
         JOIN outbox_messages o ON o.operation_id=a.operation_id
         JOIN inbound_messages i ON i.id=o.inbound_message_id WHERE i.id=$1 AND o.id=$2",
    )
    .bind(receipt.message_id)
    .bind(queued.outbox_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        state,
        (
            entry.revision + 2,
            "telegram:42".into(),
            "processed".into(),
            true
        )
    );
    let message = async |runtime: &mut ga_db::runtime_lease::RuntimeLease, id: i64, text: &str| {
        binding
            .accept_polled_update(
                runtime,
                &json!({
                    "update_id":id,"message":{"message_id":id,"date":at.unix_timestamp(),
                        "chat":{"id":42,"type":"private"},"from":{"id":42},"text":text},
                }),
                started,
                false,
            )
            .await?;
        let screen = database
            .claim_telegram_local_screen(target, started, 60)
            .await?
            .ok_or("Expected next unscreened text")?;
        database
            .apply_local_telegram_text(
                &screen,
                LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
                "en",
            )
            .await?;
        let lease = database
            .claim_telegram_model_text(target, started, 60)
            .await?
            .ok_or("Expected next screened text")?;
        Ok::<_, Box<dyn Error>>(lease)
    };
    let undo = message(&mut runtime, 902, "Undo the last synthetic change").await?;
    let TelegramModelAdmission::Ready { undo_anchor, .. } = database
        .admit_telegram_model_text(&undo, "UTC", "en")
        .await?
    else {
        return Err("Synthetic undo was not admitted".into());
    };
    assert!(undo_anchor.is_some());
    let other = database
        .create_system_event(CreateSystemEvent {
            owner_id: owner,
            authenticated_actor: "telegram:42",
            report: &note(
                "2024-01-01T12:00:00Z".parse()?,
                "Older synthetic note saved later",
            )?,
            idempotency_key: None,
            operation_id: Some(Uuid::new_v4()),
            clock_uncertainty_seconds: None,
        })
        .await?;
    let latest = database
        .interpretation_context(context_query()?)
        .await?
        .digest()?;
    let reply = |input: &InboundEnvelope, result| {
        let text = match result {
            ModelContinuationReply::Undone(1) => "Last change undone.",
            ModelContinuationReply::ContextUnknown => "The context stays unknown.",
            _ => return None,
        };
        OutboundIntent::normalize(json!({
            "owner_id":input.owner_id().to_string(),"conversation_id":input.conversation_id().to_string(),
            "channel_instance":{"channel":"telegram","instance_id":"primary"},"blocks":[{"text":text}],
        })).ok()
    };
    assert!(matches!(
        database
            .apply_telegram_model_continuation(
                &undo,
                proof(&latest),
                ModelContinuation::Undo {
                    offered_audit: undo_anchor
                },
                reply
            )
            .await,
        Err(TelegramDiaryCommitError::ModelContextChanged)
    ));
    let TelegramModelAdmission::Ready {
        undo_anchor: fresh, ..
    } = database
        .admit_telegram_model_text(&undo, "UTC", "en")
        .await?
    else {
        return Err("Synthetic undo was not readmitted".into());
    };
    assert_ne!(fresh, undo_anchor);
    database
        .apply_telegram_model_continuation(
            &undo,
            proof(&latest),
            ModelContinuation::Undo {
                offered_audit: fresh,
            },
            reply,
        )
        .await?;
    let deleted: bool = sqlx::query_scalar("SELECT deleted FROM events WHERE id=$1")
        .bind(other.id)
        .fetch_one(&pool)
        .await?;
    assert!(deleted);

    let repeated = message(&mut runtime, 903, "Undo it again").await?;
    let TelegramModelAdmission::Ready { undo_anchor, .. } = database
        .admit_telegram_model_text(&repeated, "UTC", "en")
        .await?
    else {
        return Err("Repeated undo was not admitted".into());
    };
    assert_eq!(undo_anchor, fresh);
    let digest = database
        .interpretation_context(context_query()?)
        .await?
        .digest()?;
    let queued = database
        .apply_telegram_model_continuation(
            &repeated,
            proof(&digest),
            ModelContinuation::Undo {
                offered_audit: undo_anchor,
            },
            reply,
        )
        .await?;
    let terminal: (String, String) = sqlx::query_as(
        "SELECT i.status,o.intent#>>'{blocks,0,text}' FROM outbox_messages o
         JOIN inbound_messages i ON i.id=o.inbound_message_id WHERE o.id=$1",
    )
    .bind(queued.outbox_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(terminal.0, "invalid");
    assert_eq!(
        terminal.1,
        "Не удалось применить запись или исправление. Ничего не изменено. Уточните время и детали; для отмены должна существовать предыдущая запись."
    );

    let question = Uuid::new_v4();
    sqlx::query("INSERT INTO pending_questions(id,kind,text,evidence,priority,earliest_send_at,expires_at,sent_at,status,dedup_key,attempts)
        VALUES($1,'context','Synthetic question','{}',1,$2,$3,$2,'sent','synthetic-acknowledgement',0)")
        .bind(question).bind(at-Duration::minutes(1)).bind(at+Duration::hours(1)).execute(&pool).await?;
    let answer = message(&mut runtime, 904, "I cannot tell").await?;
    let context = database.interpretation_context(context_query()?).await?;
    assert!(
        context
            .recent_questions
            .iter()
            .any(|row| row["id"] == question.to_string())
    );
    let digest = context.digest()?;
    assert!(matches!(
        database
            .apply_telegram_model_continuation(
                &answer,
                proof(&digest),
                ModelContinuation::Acknowledge {
                    question_id: Uuid::new_v4()
                },
                reply
            )
            .await,
        Err(TelegramDiaryCommitError::ExistingContextRequired)
    ));
    database
        .apply_telegram_model_continuation(
            &answer,
            proof(&digest),
            ModelContinuation::Acknowledge {
                question_id: question,
            },
            reply,
        )
        .await?;
    let status: (String, String) =
        sqlx::query_as("SELECT status,evidence->>'answer_kind' FROM pending_questions WHERE id=$1")
            .bind(question)
            .fetch_one(&pool)
            .await?;
    assert_eq!(status, ("acknowledged".into(), "unknown".into()));
    sqlx::query("DELETE FROM audit_log WHERE actor='telegram:42'")
        .execute(&pool)
        .await?;
    let empty = message(&mut runtime, 905, "Undo my previous change").await?;
    let TelegramModelAdmission::Ready { undo_anchor, .. } = database
        .admit_telegram_model_text(&empty, "UTC", "en")
        .await?
    else {
        return Err("Empty undo was not admitted".into());
    };
    assert!(undo_anchor.is_none());
    let digest = database
        .interpretation_context(context_query()?)
        .await?
        .digest()?;
    let queued = database
        .apply_telegram_model_continuation(
            &empty,
            proof(&digest),
            ModelContinuation::Undo {
                offered_audit: None,
            },
            reply,
        )
        .await?;
    let terminal: (String, String) = sqlx::query_as(
        "SELECT i.status,o.intent#>>'{blocks,0,text}' FROM outbox_messages o
         JOIN inbound_messages i ON i.id=o.inbound_message_id WHERE o.id=$1",
    )
    .bind(queued.outbox_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(terminal.0, "invalid");
    assert_eq!(
        terminal.1,
        "Не удалось применить запись или исправление. Ничего не изменено. Уточните время и детали; для отмены должна существовать предыдущая запись."
    );
    Ok(())
}
