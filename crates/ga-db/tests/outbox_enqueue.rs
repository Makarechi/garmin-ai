use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    action_tokens::{ActionTokenContext, consume_action_token},
    outbox_enqueue::{QueueIntent, QueueIntentError, queue_outbox_intent},
    share_policy::{
        DestinationKind, ShareCategory, ShareDestination, ShareRequirement, TrackerShareGrant,
    },
};
use ga_domain::{
    access::{Scope, Scopes},
    channel::OutboundIntent,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

const CONVERSATION: Uuid = Uuid::from_u128(880);
const INBOUND: Uuid = Uuid::from_u128(882);
const OPERATION: Uuid = Uuid::from_u128(883);

#[tokio::test]
#[ignore = "compares neutral outbox mutations from pinned Python on a disposable TimescaleDB"]
async fn queues_actions_once_and_rolls_back_partial_issues() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected synthetic fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(target, current);
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_OUTBOX_ENQUEUE")?)?)?;
    let now: StoredInstant = fixture["clock"]
        .as_str()
        .ok_or("Missing fixture clock")?
        .parse()?;
    seed(&pool, owner).await?;

    let cases = fixture["cases"]
        .as_array()
        .ok_or("Missing enqueue fixture")?;
    for case in cases {
        let name = case["name"].as_str().ok_or("Missing case name")?;
        let mut intent = case["intent"].clone();
        intent["owner_id"] = json!(owner.to_string());
        let intent = OutboundIntent::normalize(intent)?;
        let mut txn = pool.begin().await?;
        let result = queue_outbox_intent(
            &mut txn,
            QueueIntent {
                intent,
                now,
                operation_id: case["operation"]
                    .as_str()
                    .ok_or("Missing operation")?
                    .parse()?,
                inbound_id: case["inbound"].as_str().map(str::parse).transpose()?,
                dedup_key: case["key"].as_str(),
                requirements: &[],
            },
        )
        .await;
        if name == "expired_after_one_issue" {
            sqlx::query("INSERT INTO app_state(key,value) VALUES('synthetic-outer-commit','{}')")
                .execute(&mut *txn)
                .await?;
        }
        txn.commit().await?;
        let observed = match result {
            Ok(receipt) => json!({"id":receipt.outbox_id.to_string(),"created":receipt.created}),
            Err(QueueIntentError::Action(_)) => json!("ValueError"),
            Err(QueueIntentError::ForeignContext | QueueIntentError::ConsentRequired) => {
                json!("PermissionError")
            }
            Err(error) => return Err(format!("{name}: {error}").into()),
        };
        assert_eq!(observed, case["outcome"], "{name}");
        let mut expected = case["snapshot"].clone();
        for row in expected["outbox"]
            .as_array_mut()
            .ok_or("Invalid snapshot")?
        {
            row[6]["owner_id"] = json!(owner.to_string());
        }
        assert_eq!(snapshot(&pool).await?, expected, "{name}");
    }

    let outer_commit: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='synthetic-outer-commit')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(outer_commit);
    let signed: String = sqlx::query_scalar(
        "SELECT intent->'actions'->0->>'token' FROM outbox_messages WHERE id=$1",
    )
    .bind(Uuid::from_u128(893))
    .fetch_one(&pool)
    .await?;
    assert_eq!(signed.len(), 57);
    let context = ActionTokenContext {
        owner_id: owner,
        conversation_id: CONVERSATION,
        revision: 2,
    };
    let mut txn = pool.begin().await?;
    assert_eq!(
        consume_action_token(
            &mut txn,
            ActionTokenContext {
                revision: 1,
                ..context
            },
            &signed,
            now
        )
        .await?,
        None
    );
    assert_eq!(
        consume_action_token(&mut txn, context, &signed, now)
            .await?
            .as_deref(),
        Some("synthetic-confirm")
    );
    assert_eq!(
        consume_action_token(&mut txn, context, &signed, now).await?,
        None
    );
    txn.rollback().await?;
    consent_dependencies(&url, &pool, owner, now).await?;
    println!(
        "{} paired outbox enqueue and stored action snapshots matched",
        cases.len()
    );
    Ok(())
}

async fn consent_dependencies(
    url: &str,
    pool: &PgPool,
    owner: Uuid,
    now: StoredInstant,
) -> Result<(), Box<dyn Error>> {
    let database = Database::connect_lazy(url).map_err(|_| "Invalid fixture connection")?;
    let definition = Uuid::from_u128(995);
    let version = Uuid::from_u128(996);
    let previous_version = Uuid::from_u128(997);
    sqlx::query(
        "INSERT INTO event_definitions(id,namespace,key,owner_id,current_version,status,revision)
         VALUES($1,'user','user.synthetic_dependency',$2,2,'active',1)",
    )
    .bind(definition)
    .bind(owner)
    .execute(pool)
    .await?;
    for (id, revision) in [(previous_version, 1), (version, 2)] {
        sqlx::query(
            "INSERT INTO event_definition_versions(id,definition_id,version,schema,schema_hash,topology,
                field_metadata,labels,privacy,allowed_operations)
             VALUES($1,$2,$3,'{}','synthetic','point','{}','{}','sensitive','[]')",
        )
        .bind(id)
        .bind(definition)
        .bind(revision)
        .execute(pool)
        .await?;
    }
    let manager: Scopes = [Scope::ManageIntegrations].into_iter().collect();
    let destination = ShareDestination::new(DestinationKind::Channel, "fixture:primary")?;
    let grant = TrackerShareGrant {
        definition_id: definition,
        destination,
        categories: &[
            ShareCategory::Schema,
            ShareCategory::Facts,
            ShareCategory::OriginalText,
        ],
        granted_at: now,
    };
    database.grant_tracker_share(manager, grant).await?;
    let mut intent = json!({
        "intent_id":Uuid::from_u128(998).to_string(), "owner_id":owner.to_string(), "conversation_id":CONVERSATION.to_string(),
        "channel_instance":{"channel":"fixture","instance_id":"primary"},
        "blocks":[{"text":"synthetic tracker response"}]
    });
    let mut txn = pool.begin().await?;
    let canonical = format!("definition:{definition}");
    let noncanonical = format!("definition:{}", definition.to_string().to_uppercase());
    for (evidence, version_id, category) in [
        (json!([]), version, ShareCategory::Facts),
        (json!([canonical]), previous_version, ShareCategory::Facts),
        (json!([canonical]), version, ShareCategory::OriginalText),
        (json!([noncanonical]), version, ShareCategory::Facts),
    ] {
        intent["evidence_refs"] = evidence;
        let result = queue_outbox_intent(
            &mut txn,
            QueueIntent {
                intent: OutboundIntent::normalize(intent.clone())?,
                operation_id: OPERATION,
                inbound_id: Some(INBOUND),
                dedup_key: Some("synthetic-consent-dependency"),
                requirements: &[ShareRequirement {
                    version_id,
                    categories: &[category],
                }],
                now,
            },
        )
        .await;
        assert!(
            matches!(result, Err(QueueIntentError::ConsentRequired)),
            "accepted unrecorded dependency"
        );
    }
    intent["evidence_refs"] = json!([canonical]);
    let receipt = queue_outbox_intent(
        &mut txn,
        QueueIntent {
            intent: OutboundIntent::normalize(intent)?,
            operation_id: OPERATION,
            inbound_id: Some(INBOUND),
            dedup_key: Some("synthetic-consent-dependency"),
            requirements: &[ShareRequirement {
                version_id: version,
                categories: &[ShareCategory::Facts],
            }],
            now,
        },
    )
    .await?;
    txn.commit().await?;
    assert!(receipt.created);
    assert!(
        database
            .revoke_tracker_share(manager, definition, destination)
            .await?
    );
    let state: String = sqlx::query_scalar("SELECT state FROM outbox_messages WHERE id=$1")
        .bind(receipt.outbox_id)
        .fetch_one(pool)
        .await?;
    assert_eq!(state, "cancelled");
    Ok(())
}

async fn seed(pool: &PgPool, owner: Uuid) -> Result<(), sqlx::Error> {
    for (id, instance) in [(880, "primary"), (881, "secondary")] {
        sqlx::query(
            "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
             VALUES($1,$2,'fixture',$3,gen_random_uuid(),'{}',false)",
        )
        .bind(Uuid::from_u128(id)).bind(owner).bind(instance).execute(pool).await?;
    }
    sqlx::query(
        "INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,external_event_id,
                      sender_ref,received_at,kind,envelope,revision,status,operation_id)
         VALUES($1,$2,$3,'fixture','primary','synthetic-origin','synthetic-owner',
                 '2026-11-12T10:00:00Z','edit','{}',2,'processed',$4)",
    )
    .bind(INBOUND).bind(owner).bind(CONVERSATION).bind(OPERATION).execute(pool).await?;
    Ok(())
}

async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let raw: String = sqlx::query_scalar(
        "SELECT jsonb_build_object(
          'outbox',(SELECT coalesce(jsonb_agg(jsonb_build_array(id,operation_id,inbound_message_id,
            dedup_key,state,attempts,intent) ORDER BY id),'[]') FROM outbox_messages),
          'tokens',(SELECT count(*) FROM app_state WHERE starts_with(key,'action-token:'))
        )::text",
    )
    .fetch_one(pool)
    .await?;
    let mut data: Value = serde_json::from_str(&raw)?;
    for row in data["outbox"]
        .as_array_mut()
        .ok_or("Invalid outbox state")?
    {
        for action in row[6]["actions"]
            .as_array_mut()
            .ok_or("Invalid action list")?
        {
            let token = action["token"].as_str().ok_or("Missing signed action")?;
            assert_eq!(token.len(), 57);
            action["token"] = Value::Null;
        }
    }
    Ok(data)
}
