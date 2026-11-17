use std::{collections::HashMap, env, error::Error, fs, str::FromStr};

use ga_db::{Database, channel_ingress::IngressError};
use ga_domain::channel::InboundEnvelope;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    cases: Vec<Case>,
    messages: i64,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    input: Value,
    normalized: Option<Value>,
    stored: Option<Value>,
    error: Option<String>,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and a synthetic channel inbox fixture"]
async fn matches_pinned_inbox_and_rejects_cross_conversation_collisions()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&inspect)
        .await?;
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_CHANNEL_INGRESS")?)?)?;
    let action = Uuid::from_u128(22);
    let mut operations = HashMap::from([(action, action)]);
    let mut initial = None;
    let case_count = fixture.cases.len();
    for mut case in fixture.cases {
        case.input["owner_id"] = json!(owner.to_string());
        if case.name == "new_text" {
            initial = Some(case.input.clone());
        }
        let envelope = InboundEnvelope::parse(&serde_json::to_vec(&case.input)?);
        if case.error.as_deref() == Some("invalid") {
            assert!(envelope.is_err(), "{}", case.name);
            continue;
        }
        let envelope = envelope.map_err(|error| format!("{}: {error}", case.name))?;
        if let Some(mut normalized) = case.normalized {
            normalized["owner_id"] = json!(owner.to_string());
            assert!(
                envelope.document() == normalized,
                "{} normalization",
                case.name
            );
        }
        let result = database.ingest_channel_message(&envelope).await;
        if let Some(mut expected) = case.stored {
            let receipt = result.map_err(|error| format!("{}: {error}", case.name))?;
            let operation = operations
                .entry(receipt.operation_id)
                .or_insert(receipt.message_id);
            let (stored, status): (String, String) =
                sqlx::query_as("SELECT envelope::text,status FROM inbound_messages WHERE id=$1")
                    .bind(receipt.message_id)
                    .fetch_one(&inspect)
                    .await?;
            expected["envelope"]["owner_id"] = json!(owner.to_string());
            let actual = json!({
                "id": receipt.message_id.to_string(), "created": receipt.created, "operation": operation.to_string(),
                "supersedes": receipt.supersedes_id.map(|value| value.to_string()), "envelope": serde_json::from_str::<Value>(&stored)?,
                "status": status,
            });
            assert!(actual == expected, "{} storage", case.name);
        } else {
            let error = match result {
                Err(IngressError::ForeignConversation) => "ForeignConversation",
                Err(IngressError::MissingRevision) => "MissingRevision",
                Err(IngressError::OldRevision) => "OldRevision",
                _ => {
                    return Err(
                        format!("{}: unexpected storage result {result:?}", case.name).into(),
                    );
                }
            };
            assert_eq!(Some(error), case.error.as_deref(), "{}", case.name);
        }
    }

    let mut collision = initial.ok_or("Missing initial inbox fixture")?;
    let foreign = Uuid::from_u128(23);
    collision["conversation_id"] = json!(foreign.to_string());
    collision["message_id"] = json!(Uuid::from_u128(24).to_string());
    let parse = |input: &Value| InboundEnvelope::parse(input.to_string().as_bytes());
    assert!(matches!(
        database.ingest_channel_message(&parse(&collision)?).await,
        Err(IngressError::ForeignConversation)
    ));
    collision["external_event_id"] = json!("opaque-cross-conversation");
    collision["kind"] = json!("edit");
    collision["revision"] = json!(3);
    assert!(matches!(
        database.ingest_channel_message(&parse(&collision)?).await,
        Err(IngressError::ForeignConversation)
    ));

    let mut busy = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *busy)
        .await?;
    assert!(matches!(
        database.ingest_channel_message(&parse(&collision)?).await,
        Err(IngressError::Busy)
    ));
    busy.rollback().await?;

    sqlx::query("ALTER TABLE inbound_messages ADD CHECK(external_event_id<>'opaque-fault')")
        .execute(&inspect)
        .await?;
    collision["external_event_id"] = json!("opaque-fault");
    collision["kind"] = json!("text");
    assert!(matches!(
        database.ingest_channel_message(&parse(&collision)?).await,
        Err(IngressError::Database)
    ));
    let orphan: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM conversations WHERE id=$1)")
        .bind(foreign)
        .fetch_one(&inspect)
        .await?;
    assert!(!orphan);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM inbound_messages")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(count, fixture.messages);
    println!("{case_count} pinned cases matched with {count} stored messages");
    Ok(())
}
