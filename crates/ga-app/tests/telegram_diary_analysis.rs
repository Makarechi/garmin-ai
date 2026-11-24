use std::{collections::BTreeSet, env, error::Error, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    conversation_memory::{AnalysisReadContext, ConversationBinding},
    diary::{
        ClarificationClock, CreateSystemEvent, DiaryEventRange, InterpretationContextQuery,
        UpdateSystemEvent,
    },
    jobs::telegram_inbound::{
        TelegramDiaryCommitError, TelegramInboundTarget, TelegramModelContext,
    },
    outbox_claims::{ReplyDestination, ReplyDispatchResult},
    share_policy::{DestinationKind, ShareDestination},
};
use ga_domain::{
    channel::OutboundIntent,
    event_input::{EventInput, ValidationMode},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

fn note(at: StoredInstant, text: &str) -> Result<EventInput, Box<dyn Error>> {
    Ok(EventInput::from_json(
        &serde_json::to_vec(&json!({
            "start":at.to_string(),"timezone":"UTC","source":"manual",
            "payload":{"type":"note","description":text}
        }))?,
        32_000,
        ValidationMode::NewReport,
    )?)
}

#[tokio::test]
#[ignore = "uses an isolated migrated TimescaleDB and a synthetic Telegram dispatch"]
async fn analysis_rejects_edited_historical_data_before_queue_and_before_send()
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
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(expected, actual);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    sqlx::query("UPDATE people SET timezone='UTC' WHERE singleton")
        .execute(&pool)
        .await?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES('preferences:onboarding',
        '{\"model_categories\":[\"diary\"],\"channel\":{\"channel\":\"telegram\",\"instance_id\":\"primary\"},\"fallback_channels\":[],\"timezone\":\"UTC\",\"locale\":\"en\"}'::jsonb)")
        .execute(&pool).await?;
    sqlx::query("INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')").bind(owner).execute(&pool).await?;
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let started = StoredInstant::try_from(now)?;
    let message_at =
        StoredInstant::try_from(OffsetDateTime::from_unix_timestamp(now.unix_timestamp())?)?;
    let when = "2024-01-01T12:00:00Z".parse()?;
    let old = database
        .create_system_event(CreateSystemEvent {
            owner_id: owner,
            authenticated_actor: "synthetic-fixture",
            report: &note(when, "Synthetic historical note")?,
            idempotency_key: None,
            operation_id: None,
            clock_uncertainty_seconds: None,
        })
        .await?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let telegram = TelegramBinding {
        owner_id: owner,
        instance_id: "primary",
        external_owner_id: 42,
    };
    telegram
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":850,"message":{
                "message_id":850,"from":{"id":42},"chat":{"id":42,"type":"private"},
                "text":"What did I record on the synthetic date?","date":now.unix_timestamp()
            }}),
            started,
            false,
        )
        .await?;
    let lease = database
        .claim_telegram_inbound(
            TelegramInboundTarget {
                owner_id: owner,
                instance_id: "primary",
            },
            started,
            60,
        )
        .await?
        .ok_or("Expected authenticated native message")?;
    let channel = ConversationBinding {
        owner_id: owner,
        conversation_id: ga_domain::channel::telegram_conversation_id(owner, "primary", 42),
    };
    let model_id = "model:gemini:primary";
    let model = ShareDestination::new(DestinationKind::Model, model_id)?;
    let context = database
        .interpretation_context(InterpretationContextQuery {
            binding: channel,
            model,
            now: message_at,
            mentioned_events: &[],
            clarification_clock: ClarificationClock {
                message_at,
                conversation_now: started,
                use_message_time: false,
            },
        })
        .await?;
    assert_eq!(context.events.entries, [] as [serde_json::Value; 0]);
    let digest = context.digest()?;
    let policy = TelegramModelContext {
        model_instance: model_id,
        timezone: "UTC",
        configured_timezone: "UTC",
        mentioned_events: &[],
        digest: &digest,
    };
    let goals_revision = database.personal_goals(owner).await?.revision;
    let read_context = AnalysisReadContext {
        binding: channel,
        inbound_id: lease.message_id(),
        operation_id: lease.operation_id(),
        memory: &context.memory_fence,
        goals_revision,
    };
    let arguments =
        json!({"start":"2024-01-01T00:00:00Z","end":"2024-01-02T00:00:00Z","kind":"note"});
    let read = async || -> Result<Value, Box<dyn Error>> {
        let range = DiaryEventRange::from_analysis_arguments(
            arguments.as_object().ok_or("Expected arguments")?,
        )
        .ok_or("Invalid fixture range")?;
        let page = database
            .list_analysis_events(read_context, model, range)
            .await?;
        Ok(json!({"tool":"events","arguments":arguments,"result":page.value}))
    };
    let first = read().await?;
    let edit = async |revision, description: &str| -> Result<_, Box<dyn Error>> {
        Ok(database
            .update_system_event(UpdateSystemEvent {
                owner_id: owner,
                authenticated_actor: "api",
                event_id: old.id,
                expected_revision: revision,
                report: &note(when, description)?,
                operation_id: None,
            })
            .await?)
    };
    let changed = edit(old.revision, "Synthetic edited history").await?;
    let reply = || {
        OutboundIntent::normalize(json!({
            "owner_id":owner.to_string(),"conversation_id":channel.conversation_id.to_string(),
            "channel_instance":{"channel":"telegram","instance_id":"primary"},
            "blocks":[{"text":"Synthetic cited answer"}],
        }))
    };
    assert!(matches!(
        database
            .queue_telegram_diary_analysis(
                &lease,
                policy,
                goals_revision,
                &[first],
                &BTreeSet::default(),
                reply()?
            )
            .await,
        Err(TelegramDiaryCommitError::ModelContextChanged)
    ));
    let current_evidence = read().await?;
    sqlx::query(
        "UPDATE app_state SET value=jsonb_set(value,'{model_categories}','[\"health\"]'::jsonb)
        WHERE key='preferences:onboarding'",
    )
    .execute(&pool)
    .await?;
    assert!(matches!(
        database
            .queue_telegram_diary_analysis(
                &lease,
                policy,
                goals_revision,
                &[current_evidence],
                &BTreeSet::default(),
                reply()?
            )
            .await,
        Err(TelegramDiaryCommitError::ModelContextChanged)
    ));
    sqlx::query(
        "UPDATE app_state SET value=jsonb_set(value,'{model_categories}','[\"diary\"]'::jsonb)
        WHERE key='preferences:onboarding'",
    )
    .execute(&pool)
    .await?;
    let queued = database
        .queue_telegram_diary_analysis(
            &lease,
            policy,
            goals_revision,
            &[read().await?],
            &BTreeSet::default(),
            reply()?,
        )
        .await?;
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "telegram",
        instance_id: "primary",
    };
    let claim = database
        .claim_outbox_reply(destination(), started, 60)
        .await?
        .ok_or("Expected current reply")?;
    assert_eq!(claim.outbox_id, queued.outbox_id);
    edit(
        changed.revision,
        "Synthetic history changed before delivery",
    )
    .await?;
    assert_eq!(
        database
            .dispatch_telegram_outbox_reply(destination(), 42, claim, &[], |_| async {
                panic!("Stale evidence escaped to Telegram")
            })
            .await?,
        ReplyDispatchResult::Cancelled
    );
    let state: String = sqlx::query_scalar("SELECT state FROM outbox_messages WHERE id=$1")
        .bind(queued.outbox_id)
        .fetch_one(&pool)
        .await?;
    assert_eq!(state, "cancelled");
    Ok(())
}
