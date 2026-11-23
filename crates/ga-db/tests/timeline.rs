use std::{collections::BTreeMap, env, error::Error, fs, future::ready, io, str::FromStr};

use ga_db::{
    Database,
    conversation_memory::{AnalysisSubject, ConversationBinding, MemoryFence},
    diary::{DefinitionPage, DefinitionReadFence, EVENT_DEFINITIONS, TimelineReadFence},
    health_analysis::{AnalysisTimelineQuery, HealthAnalysisError, TimelineError},
    share_policy::{
        AnalysisShareEvidence, DestinationKind, ModelChannelSharing, ModelConversation,
        ShareDestination,
    },
};
use ga_domain::model_consent::ModelCategories;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    start: String,
    end: String,
    name: String,
    error: Option<String>,
    expected: Option<Value>,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB timeline fixtures"]
async fn compares_layered_timelines() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected disposable TimescaleDB URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Database must end in _test"))?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(&actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_TIMELINE")?)?)?;
    for case in cases {
        let start = OffsetDateTime::parse(&case.start, &Rfc3339)?;
        let end = OffsetDateTime::parse(&case.end, &Rfc3339)?;
        let actual = database.read_owner_timeline(start, end).await;
        if let Some(expected_error) = case.error {
            assert_eq!(
                actual.err().map(|error| error.to_string()),
                Some(expected_error),
                "{}",
                case.name,
            );
        } else {
            let actual: Value = serde_json::from_slice(&actual?)?;
            let expected = case.expected.ok_or("Fixture has no expected timeline")?;
            for key in ["segments", "layers", "events", "semantics"] {
                let actual = &actual[key];
                let expected = &expected[key];
                // Python encodes a few fixed 0 or 1 fields as integers. JSON clients compare
                // their numeric values, even when the native floating-point output has a .0.
                assert!(
                    equivalent(actual, expected),
                    "{} differs at {key}: {}",
                    case.name,
                    compact_difference(actual, expected)
                );
            }
        }
    }
    check_timeline_before_model_delivery(&database, &pool).await?;
    let start = OffsetDateTime::parse("2026-10-25T09:00:00Z", &Rfc3339)?;
    let end = OffsetDateTime::parse("2026-10-25T10:00:00Z", &Rfc3339)?;
    assert!(matches!(
        database.read_owner_timeline(start, end).await,
        Err(TimelineError::Health(HealthAnalysisError::ReplayPending))
    ));
    Ok(())
}

async fn check_timeline_before_model_delivery(
    database: &Database,
    pool: &PgPool,
) -> Result<(), Box<dyn Error>> {
    let owner_id = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(pool)
        .await?;
    let conversation_id = Uuid::new_v4();
    let epoch = sqlx::query_scalar(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch)
         VALUES($1,$2,'web','local',gen_random_uuid()) RETURNING memory_epoch",
    )
    .bind(conversation_id)
    .bind(owner_id)
    .fetch_one(pool)
    .await?;
    let memory = MemoryFence {
        target_epoch: epoch,
        source_epochs: BTreeMap::default(),
    };
    let subject = AnalysisSubject::Web {
        binding: ConversationBinding {
            owner_id,
            conversation_id,
        },
        memory: &memory,
        goals_revision: database.personal_goals(owner_id).await?.revision,
    };
    let model = ShareDestination::new(DestinationKind::Model, "model:openai:timeline-fixture")?;
    let arguments = json!({"start": "2026-10-25T09:00:00Z", "end": "2026-10-25T10:00:00Z"});
    let query =
        AnalysisTimelineQuery::from_arguments(arguments.as_object().ok_or("Expected object")?)
            .ok_or("Expected timeline query")?;
    let result = database
        .read_analysis_timeline(subject, model, &query)
        .await?;
    assert!(result.tracker_versions.is_empty());
    assert!(
        result.value["segments"]
            .as_array()
            .is_some_and(|rows| !rows.is_empty())
    );
    let timeline = [TimelineReadFence::from_read(&json!({
        "tool": "timeline", "arguments": arguments, "result": result.value,
    }))
    .ok_or("Expected saved timeline evidence")?];
    let evidence = AnalysisShareEvidence {
        timeline: timeline.into(),
        ..Default::default()
    };
    let channel = ModelChannelSharing {
        destination: ShareDestination::new(DestinationKind::Channel, "web:local")?,
        requirements: &[],
        conversation: Some(ModelConversation::Analysis {
            subject,
            evidence: &evidence,
        }),
    };
    let categories = ModelCategories::HEALTH_AND_DIARY;
    let send = || database.with_model_delivery(model, categories, &[], Some(channel), || ready(()));
    assert_eq!(
        send().await?,
        Some(()),
        "Current timeline should reach the sender"
    );
    assert_eq!(
        sqlx::query(
            "UPDATE module_configs SET llm_enabled=false WHERE owner_id=$1 AND pack_key='training'"
        )
        .bind(owner_id)
        .execute(pool)
        .await?
        .rows_affected(),
        1
    );
    assert_eq!(
        send().await?,
        None,
        "A revoked pack must block the next model request"
    );
    sqlx::query(
        "UPDATE module_configs SET llm_enabled=true WHERE owner_id=$1 AND pack_key='training'",
    )
    .bind(owner_id)
    .execute(pool)
    .await?;
    assert_eq!(send().await?, Some(()));
    sqlx::query("UPDATE timeline_intervals SET label='changed calendar label' WHERE id='plan'")
        .execute(pool)
        .await?;
    assert_eq!(
        send().await?,
        None,
        "Changed timeline data must block the next model request"
    );
    sqlx::query("UPDATE timeline_intervals SET label='meeting' WHERE id='plan'")
        .execute(pool)
        .await?;
    assert_eq!(send().await?, Some(()));
    let arguments = json!({"definition_key": "system.note"});
    let page = DefinitionPage::parse(arguments.as_object().ok_or("Expected object")?)
        .ok_or("Expected definition query")?;
    let saved = database
        .read_analysis_definitions(subject, model, page)
        .await?;
    assert!(saved.schema_versions.is_empty());
    assert_eq!(saved.value["rows"][0]["key"], "system.note");
    let definitions = [DefinitionReadFence::from_read(&json!({
        "tool": EVENT_DEFINITIONS, "arguments": arguments, "result": saved.value,
    }))
    .ok_or("Expected saved definition evidence")?];
    let evidence = AnalysisShareEvidence {
        definitions: definitions.into(),
        ..Default::default()
    };
    let definition_sharing = ModelChannelSharing {
        conversation: Some(ModelConversation::Analysis {
            subject,
            evidence: &evidence,
        }),
        ..channel
    };
    let send_definition = || {
        database.with_model_delivery(model, categories, &[], Some(definition_sharing), || {
            ready(())
        })
    };
    assert_eq!(send_definition().await?, Some(()));
    sqlx::query("UPDATE event_definitions SET revision=revision+1 WHERE key='system.note'")
        .execute(pool)
        .await?;
    assert_eq!(
        send_definition().await?,
        None,
        "Changed definition must block the next model request"
    );
    sqlx::query(
        "INSERT INTO source_payloads
         (id,source,endpoint,source_key,payload_hash,archive_key,fetched_at,parser_version,status,payload)
         VALUES(gen_random_uuid(),'garmin_connect','daily','2026-10-25',repeat('a',64),
                'synthetic',clock_timestamp(),0,'pending','{}')",
    )
    .execute(pool)
    .await?;
    assert_eq!(
        send().await?,
        None,
        "Replay must block the next model request"
    );
    println!("Blocked model sends after timeline pack revocation, changed data and pending replay");
    Ok(())
}

fn equivalent(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(left), Value::Number(right)) if left != right => {
            left.as_f64() == right.as_f64()
        }
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| equivalent(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .all(|(key, left)| right.get(key).is_some_and(|right| equivalent(left, right)))
        }
        _ => left == right,
    }
}

fn compact_difference(left: &Value, right: &Value) -> String {
    match (left, right) {
        (Value::Array(left), Value::Array(right)) => {
            if let Some((i, (left, right))) = left
                .iter()
                .zip(right)
                .enumerate()
                .find(|(_, (l, r))| !equivalent(l, r))
            {
                format!("item {i}: {}", compact_difference(left, right))
            } else {
                format!("received {} items, expected {}", left.len(), right.len())
            }
        }
        (Value::Object(left), Value::Object(right)) => {
            if let Some((key, left)) = left
                .iter()
                .find(|(key, left)| !right.get(*key).is_some_and(|right| equivalent(left, right)))
            {
                format!(
                    "{key}: {}",
                    compact_difference(left, right.get(key).unwrap_or(&Value::Null))
                )
            } else {
                format!("received {} fields, expected {}", left.len(), right.len())
            }
        }
        _ => json!({"native": left,"pinned": right})
            .to_string()
            .chars()
            .take(256)
            .collect(),
    }
}
