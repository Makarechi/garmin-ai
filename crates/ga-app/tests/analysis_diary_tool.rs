use std::{collections::BTreeSet, env, error::Error, fs, str::FromStr};

use ga_app::{
    model_gateway::{AnalysisReadTools, DiaryAnalysisTools},
    telegram_input::TelegramBinding,
};
use ga_db::{
    Database,
    conversation_memory::{AnalysisReadContext, ConversationBinding},
    personal_goals::{GoalSelection, PersonalGoal},
};
use ga_domain::{model_consent::ModelCategories, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

#[tokio::test]
#[ignore = "requires the seeded analysis diary policy fixture on disposable TimescaleDB"]
async fn diary_tool_is_bound_to_the_current_question() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Invalid fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected a disposable database")?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, target);
    let owner_id = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&inspect)
        .await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_ANALYSIS_DIARY")?)?)?;
    let args = fixture["args"]
        .as_object()
        .ok_or("Missing model arguments")?;
    let now: StoredInstant = "2026-10-25T10:00:00Z".parse()?;
    for case in fixture["cases"]
        .as_array()
        .ok_or("Missing reference cases")?
    {
        let index = u128::from(case["index"].as_u64().ok_or("Missing question index")?);
        let binding = ConversationBinding {
            owner_id,
            conversation_id: Uuid::from_u128(100 + index),
        };
        let memory = db.conversation_memory(binding, now).await?;
        let context = AnalysisReadContext {
            binding,
            inbound_id: Uuid::from_u128(200 + index),
            operation_id: Uuid::from_u128(300 + index),
            memory: &memory.fence,
            goals_revision: 0,
        };
        let model = case["model"].as_str().ok_or("Missing model destination")?;
        let reader = DiaryAnalysisTools::new(&db, context, model)?;
        assert!(reader.context_current().await);
        assert_eq!(reader.categories(), ModelCategories::DIARY);
        let menu = reader.offered_tools();
        let names: Vec<_> = menu.iter().map(|tool| tool.name).collect();
        assert_eq!(
            names,
            [
                "events",
                "event_definitions",
                "resource_catalog",
                "generic_analysis"
            ]
        );
        assert_eq!(*menu[0].description, fixture["menu"]);
        let result = reader
            .read("events", args)
            .await?
            .ok_or("Expected visible diary page")?;
        assert_eq!(result.value, case["result"]);
        assert!(!result.value.to_string().contains("synthetic quote"));
        let versions = result.value["rows"]
            .as_array()
            .ok_or("Missing diary rows")?
            .iter()
            .filter(|row| {
                row["kind"]
                    .as_str()
                    .is_some_and(|kind| kind.starts_with("user."))
            })
            .map(|row| {
                row["definition_version_id"]
                    .as_str()
                    .ok_or("Missing definition version")?
                    .parse::<Uuid>()
                    .map_err(|_| "Invalid definition version")
            })
            .collect::<Result<BTreeSet<_>, _>>()?;
        assert!(!versions.is_empty());
        assert_eq!(result.tracker_versions, versions);
        assert!(reader.read("update_event", args).await?.is_none());
        let mut injected = args.clone();
        injected.insert("channel".into(), json!("telegram:allowed"));
        assert!(reader.read("events", &injected).await?.is_none());
    }

    verify_native_ingress(&db, &inspect, owner_id, now, &fixture).await?;

    let binding = ConversationBinding {
        owner_id,
        conversation_id: Uuid::from_u128(100),
    };
    let memory = db.conversation_memory(binding, now).await?;
    let context = AnalysisReadContext {
        binding,
        inbound_id: Uuid::from_u128(200),
        operation_id: Uuid::from_u128(300),
        memory: &memory.fence,
        goals_revision: 0,
    };
    let reader = DiaryAnalysisTools::new(&db, context, "model:primary")?;
    let foreign = DiaryAnalysisTools::new(
        &db,
        AnalysisReadContext {
            binding: ConversationBinding {
                owner_id: Uuid::nil(),
                ..binding
            },
            ..context
        },
        "model:primary",
    )?;
    assert!(!foreign.context_current().await && foreign.read("events", args).await?.is_none());
    sqlx::query(
        "UPDATE app_state SET value=value || jsonb_build_object('model_categories','[\"health\"]'::jsonb)
                 WHERE key='preferences:onboarding'",
    )
    .execute(&inspect)
    .await?;
    assert!(!reader.context_current().await && reader.read("events", args).await?.is_none());
    sqlx::query(
        "UPDATE app_state SET value=value || jsonb_build_object('model_categories','[\"diary\"]'::jsonb)
                 WHERE key='preferences:onboarding'",
    )
    .execute(&inspect)
    .await?;
    sqlx::query(
        "UPDATE channel_bindings SET external_id='999999' WHERE channel_instance_id='allowed'",
    )
    .execute(&inspect)
    .await?;
    assert!(!reader.context_current().await && reader.read("events", args).await?.is_none());
    sqlx::query(
        "UPDATE channel_bindings SET external_id='900001' WHERE channel_instance_id='allowed'",
    )
    .execute(&inspect)
    .await?;
    assert!(reader.context_current().await);

    db.forget_conversation_memory(ConversationBinding {
        conversation_id: Uuid::from_u128(101),
        ..binding
    })
    .await?;
    assert!(!reader.context_current().await && reader.read("events", args).await?.is_none());
    let memory = db.conversation_memory(binding, now).await?;
    let context = AnalysisReadContext {
        memory: &memory.fence,
        ..context
    };
    let reader = DiaryAnalysisTools::new(&db, context, "model:primary")?;
    assert!(reader.context_current().await);
    let goals = db
        .select_personal_goals(
            owner_id,
            GoalSelection {
                expected_revision: 0,
                goals: &[PersonalGoal::Sleep],
                now,
                command: None,
            },
        )
        .await?;
    assert!(!reader.context_current().await && reader.read("events", args).await?.is_none());
    let reader = DiaryAnalysisTools::new(
        &db,
        AnalysisReadContext {
            goals_revision: goals.revision,
            ..context
        },
        "model:primary",
    )?;
    assert!(reader.context_current().await);
    sqlx::query(
        "INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,
           external_event_id,sender_ref,received_at,kind,envelope,revision,operation_id,supersedes_id)
         SELECT gen_random_uuid(),owner_id,conversation_id,channel,channel_instance_id,
           external_event_id,sender_ref,received_at,'edit','{}',2,operation_id,id
         FROM inbound_messages WHERE id=$1",
    ).bind(context.inbound_id).execute(&inspect).await?;
    assert!(!reader.context_current().await && reader.read("events", args).await?.is_none());
    Ok(())
}

async fn verify_native_ingress(
    db: &Database,
    inspect: &PgPool,
    owner_id: Uuid,
    now: StoredInstant,
    fixture: &Value,
) -> Result<(), Box<dyn Error>> {
    let channel = TelegramBinding {
        owner_id,
        instance_id: "allowed",
        external_owner_id: 900_001,
    };
    let arrival = json!({
        "update_id":900_010,
        "message":{"message_id":900_010,"date":OffsetDateTime::from(now).unix_timestamp(),
            "from":{"id":900_001},"chat":{"id":900_001,"type":"private"},
            "text":"Synthetic question through native ingress"},
    });
    let mut runtime = db.acquire_runtime_lease().await?;
    let receipt = channel
        .accept_polled_update(&mut runtime, &arrival, now, false)
        .await?
        .inbound
        .ok_or("Native ingress did not store the question")?;
    let (conversation_id, external): (Uuid, Option<String>) = sqlx::query_as(
        "SELECT message.conversation_id,conversation.external_conversation_id FROM inbound_messages message
         JOIN conversations conversation ON conversation.id=message.conversation_id WHERE message.id=$1",
    ).bind(receipt.message_id).fetch_one(inspect).await?;
    assert!(external.is_none());
    let binding = ConversationBinding {
        owner_id,
        conversation_id,
    };
    let memory = db.conversation_memory(binding, now).await?;
    let context = AnalysisReadContext {
        binding,
        inbound_id: receipt.message_id,
        operation_id: receipt.operation_id,
        memory: &memory.fence,
        goals_revision: 0,
    };
    let reader = DiaryAnalysisTools::new(db, context, "model:primary")?;
    let args = fixture["args"]
        .as_object()
        .ok_or("Missing model arguments")?;
    assert!(reader.context_current().await);
    assert_eq!(
        reader
            .read("events", args)
            .await?
            .ok_or("Native diary read was rejected")?
            .value,
        fixture["cases"][0]["result"]
    );
    let selected: String =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='preferences:onboarding'")
            .fetch_one(inspect)
            .await?;
    sqlx::query("UPDATE app_state SET value=value-'channel'-'fallback_channels' WHERE key='preferences:onboarding'")
        .execute(inspect).await?;
    assert!(!reader.context_current().await && reader.read("events", args).await?.is_none());
    sqlx::query("UPDATE app_state SET value=$1::jsonb WHERE key='preferences:onboarding'")
        .bind(selected)
        .execute(inspect)
        .await?;
    assert!(reader.context_current().await);
    Ok(())
}
