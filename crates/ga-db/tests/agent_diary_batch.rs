use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    diary::{CreateAgentLogBatch, DiaryWriteError},
};
use ga_domain::{agent_interpretation::AgentInterpretation, event_input::EventSource};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "compares event batches and derived facts in disposable TimescaleDB"]
async fn preserves_order_and_rolls_back_related_diary_reports() -> Result<(), Box<dyn Error>> {
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
    assert_eq!(actual, expected);
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_AGENT_DIARY_BATCH")?)?)?;
    let exact_text = fixture["text"].as_str().ok_or("Missing message evidence")?;
    let cases = fixture["cases"].as_array().ok_or("Missing batch cases")?;
    let mut prior = None;
    for case in cases {
        let name = case["name"].as_str().ok_or("Missing case name")?;
        let command = AgentInterpretation::from_value(&case["command"])?;
        let stored = database
            .create_agent_log_batch(CreateAgentLogBatch {
                owner_id,
                command: &command,
                authenticated_actor: "owner",
                message_text: exact_text,
                source: EventSource::TelegramText,
                operation_id: case["operation"]
                    .as_str()
                    .ok_or("Missing operation")?
                    .parse()?,
                idempotency_prefix: case["prefix"].as_str().ok_or("Missing prefix")?,
            })
            .await;
        let result = match stored {
            Ok(entries) => {
                let ids: Vec<_> = entries.iter().map(|entry| entry.id).collect();
                if name == "repeat" {
                    assert_eq!(prior.as_ref(), Some(&ids));
                    assert!(entries.iter().all(|entry| entry.reused));
                } else if name == "linked" {
                    prior = Some(ids);
                }
                json!(
                    entries
                        .iter()
                        .map(|entry| &entry.snapshot["idempotency_key"])
                        .collect::<Vec<_>>()
                )
            }
            Err(DiaryWriteError::InvalidRelation) => Value::Null,
            Err(error) => return Err(error.into()),
        };
        assert_eq!(result, case["order"], "{name}");
        let state: String = sqlx::query_scalar(
            "SELECT jsonb_build_object(
                'events',(SELECT coalesce(jsonb_agg(jsonb_build_object(
                    'key',e.idempotency_key,'kind',e.kind,'source',e.source,'status',e.status,
                    'text',e.original_text,'parent_key',p.idempotency_key) ORDER BY e.idempotency_key),'[]'::jsonb)
                    FROM events e LEFT JOIN events p ON p.id::text=e.payload->>'reason_event_id'),
                'audits',(SELECT coalesce(jsonb_agg(jsonb_build_object(
                    'key',e.idempotency_key,'action',a.action,'operation',a.operation_id,'actor',a.actor)
                    ORDER BY a.id),'[]'::jsonb) FROM audit_log a JOIN events e ON e.id=a.event_id),
                'metrics',(SELECT count(*) FROM metric_observations),
                'intervals',(SELECT count(*) FROM timeline_intervals)
            )::text",
        ).fetch_one(&pool).await?;
        assert_eq!(
            serde_json::from_str::<Value>(&state)?,
            case["state"],
            "{name}"
        );
    }
    println!(
        "{} paired diary batch outcomes and stored state snapshots matched",
        cases.len()
    );
    Ok(())
}
