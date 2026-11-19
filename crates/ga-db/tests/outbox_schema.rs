use std::{
    env,
    error::Error,
    future::ready,
    str::FromStr,
    sync::atomic::{AtomicUsize, Ordering},
};

use ga_db::{
    Database,
    delivery::DeliveryState,
    outbox_claims::{ReplyDestination, ReplyDispatchResult, ReplyObservation, ReplySendOutcome},
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
use time::OffsetDateTime;
use uuid::Uuid;

const DEFINITION: Uuid = Uuid::from_u128(51801);
const VERSION: Uuid = Uuid::from_u128(51802);
const CONVERSATION: Uuid = Uuid::from_u128(51803);

async fn clock(pool: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    Ok(StoredInstant::try_from(
        sqlx::query_scalar::<_, OffsetDateTime>("SELECT clock_timestamp()")
            .fetch_one(pool)
            .await?,
    )?)
}

fn intent(owner: Uuid, refs: &[String]) -> Result<OutboundIntent, Box<dyn Error>> {
    Ok(OutboundIntent::normalize(
        json!({"owner_id":owner.to_string(),"conversation_id":CONVERSATION.to_string(),"channel_instance":{"channel":"fixture","instance_id":"primary"},"blocks":[{"text":"Synthetic permitted tracker schema"}],"evidence_refs":refs}),
    )?)
}

async fn enqueue(
    pool: &PgPool,
    owner: Uuid,
    refs: &[String],
    categories: &[ShareCategory],
) -> Result<Result<Uuid, QueueIntentError>, Box<dyn Error>> {
    let mut txn = pool.begin().await?;
    let requirement = [ShareRequirement {
        version_id: VERSION,
        categories,
    }];
    let result = queue_outbox_intent(
        &mut txn,
        QueueIntent {
            intent: intent(owner, refs)?,
            operation_id: Uuid::new_v4(),
            inbound_id: None,
            dedup_key: None,
            requirements: &requirement,
            now: clock(pool).await?,
        },
    )
    .await
    .map(|receipt| receipt.outbox_id);
    txn.commit().await?;
    Ok(result)
}

#[tokio::test]
#[ignore = "verifies schema-only tracker reply delivery and revocation in disposable TimescaleDB"]
async fn schema_only_delivery_preserves_exact_version_and_fact_boundaries()
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
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT current_database()")
            .fetch_one(&pool)
            .await?,
        expected
    );
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query("INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch) VALUES($1,$2,'fixture','primary',gen_random_uuid())").bind(CONVERSATION).bind(owner).execute(&pool).await?;
    sqlx::query("INSERT INTO event_definitions(id,owner_id,namespace,key,status,current_version,revision) VALUES($1,$2,'user','user.synthetic_schema_reply','active',1,1)").bind(DEFINITION).bind(owner).execute(&pool).await?;
    sqlx::query("INSERT INTO event_definition_versions(id,definition_id,version,schema,schema_hash,topology,field_metadata,labels,privacy,allowed_operations) VALUES($1,$2,1,'{}','synthetic','point','{}','{}','sensitive','[\"query\"]')").bind(VERSION).bind(DEFINITION).execute(&pool).await?;
    let managers: Scopes = [Scope::ManageIntegrations].into_iter().collect();
    let target = ShareDestination::new(DestinationKind::Channel, "fixture:primary")?;
    database
        .grant_tracker_share(
            managers,
            TrackerShareGrant {
                definition_id: DEFINITION,
                destination: target,
                categories: &[ShareCategory::Schema],
                granted_at: clock(&pool).await?,
            },
        )
        .await?;
    let definition = format!("definition:{DEFINITION}");
    let schema = format!("definition-schema-version:{VERSION}");
    let facts = format!("definition-version:{VERSION}");
    let refs = vec![definition.clone(), schema.clone()];
    let outbox = enqueue(&pool, owner, &refs, &[ShareCategory::Schema]).await??;
    for (refs, categories) in [
        (
            vec![definition.clone(), facts.clone()],
            vec![ShareCategory::Schema, ShareCategory::Facts],
        ),
        (
            vec![definition.clone(), schema.clone(), facts.clone()],
            vec![ShareCategory::Schema],
        ),
        (
            vec![definition.clone(), schema.clone()],
            vec![ShareCategory::Facts],
        ),
        (
            vec![definition.clone(), schema.clone()],
            vec![ShareCategory::OriginalText],
        ),
        (vec![schema.clone()], vec![ShareCategory::Schema]),
        (
            vec![
                definition.clone(),
                "definition-schema-version:invalid".into(),
            ],
            vec![ShareCategory::Schema],
        ),
        (
            vec![
                definition.clone(),
                format!("definition-schema-version:{}", Uuid::from_u128(51809)),
            ],
            vec![ShareCategory::Schema],
        ),
    ] {
        assert!(
            matches!(
                enqueue(&pool, owner, &refs, &categories).await?,
                Err(QueueIntentError::ConsentRequired)
            ),
            "{refs:?}"
        );
    }
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "fixture",
        instance_id: "primary",
    };
    let claimed = database
        .claim_outbox_reply(destination(), clock(&pool).await?, 60)
        .await?
        .ok_or("Expected schema-only reply claim")?;
    assert_eq!(claimed.outbox_id, outbox);
    let sends = AtomicUsize::new(0);
    let observed = clock(&pool).await?;
    let result = database
        .dispatch_outbox_reply(destination(), claimed, &[], |body: Value| {
            assert_eq!(body["evidence_refs"], json!(refs));
            sends.fetch_add(1, Ordering::Relaxed);
            ready(Some(ReplySendOutcome::Observation(ReplyObservation {
                state: DeliveryState::ProviderAccepted,
                observed_at: observed,
                provider_reference: Some("synthetic-schema-delivery".into()),
                detail: None,
            })))
        })
        .await?;
    assert!(matches!(
        result,
        ReplyDispatchResult::Recorded {
            state: DeliveryState::ProviderAccepted,
            ..
        }
    ));
    let revoked = enqueue(&pool, owner, &refs, &[ShareCategory::Schema]).await??;
    let claim = database
        .claim_outbox_reply(destination(), clock(&pool).await?, 60)
        .await?
        .ok_or("Expected revocable schema reply")?;
    assert_eq!(claim.outbox_id, revoked);
    assert!(
        database
            .revoke_tracker_share(managers, DEFINITION, target)
            .await?
    );
    let result = database
        .dispatch_outbox_reply(destination(), claim, &[], |_| {
            sends.fetch_add(1, Ordering::Relaxed);
            ready(None)
        })
        .await?;
    assert_eq!(result, ReplyDispatchResult::Cancelled);
    assert_eq!(sends.load(Ordering::Relaxed), 1);
    assert!(matches!(
        enqueue(&pool, owner, &refs, &[ShareCategory::Schema]).await?,
        Err(QueueIntentError::ConsentRequired)
    ));
    println!(
        "Schema-only reply delivered once, refused fact escalation and malformed references, and blocked delivery after revocation"
    );
    Ok(())
}
