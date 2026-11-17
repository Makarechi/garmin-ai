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
    outbox_claims::{ReplyClaim, ReplyDestination, ReplyDispatchResult, ReplyObservation},
    share_policy::{DestinationKind, ShareCategory, ShareDestination, TrackerShareGrant},
};
use ga_domain::{
    access::{Scope, Scopes},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

const CONVERSATION: Uuid = Uuid::from_u128(280);
const OPERATION: Uuid = Uuid::from_u128(282);
const EPOCH: Uuid = Uuid::from_u128(283);
const INITIAL: Uuid = Uuid::from_u128(301);
const DEFINITION: Uuid = Uuid::from_u128(1410);

#[tokio::test]
#[ignore = "exercises the outbox send fence and storage recovery in disposable TimescaleDB"]
async fn retires_dispatch_tokens_and_blocks_stale_or_unapproved_content()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected test database URL")?;
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
    assert_eq!(current, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "fixture",
        instance_id: "primary",
    };
    sqlx::query(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
         VALUES($1,$2,'fixture','primary',$3,'{}',false)",
    ).bind(CONVERSATION).bind(owner).bind(EPOCH).execute(&pool).await?;
    inbound(&pool, owner, INITIAL, 1).await?;
    let expected = fixture_intent(owner, 1001);
    let ordinary = queue(&pool, owner, expected.clone(), None).await?;
    let claim = claim_for(&pool, &database, destination(), ordinary).await?;
    let observed = clock(&pool).await?;
    let delivered = AtomicUsize::new(0);
    let send_database = &database;
    let send_pool = &pool;
    let send_count = &delivered;
    let result = database
        .dispatch_outbox_reply(destination(), claim, &[], |body| async move {
            assert_eq!(body, expected);
            send_count.fetch_add(1, Ordering::Relaxed);
            for key in [72_104_619_i64, 72_104_631, 72_104_622] {
                let mut writer = send_pool.begin().await.ok()?;
                let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                    .bind(key)
                    .fetch_one(&mut *writer)
                    .await
                    .ok()?;
                assert!(!acquired);
                writer.rollback().await.ok()?;
            }
            let retry = send_database
                .dispatch_outbox_reply(destination(), claim, &[], |_| {
                    send_count.fetch_add(1, Ordering::Relaxed);
                    ready(None)
                })
                .await
                .ok()?;
            assert_eq!(retry, ReplyDispatchResult::ClaimUnavailable);
            Some(ReplyObservation {
                state: DeliveryState::ProviderAccepted,
                observed_at: observed,
                provider_reference: Some("opaque/synthetic-accepted".into()),
                detail: None,
            })
        })
        .await?;
    assert!(matches!(
        result,
        ReplyDispatchResult::Recorded {
            state: DeliveryState::ProviderAccepted,
            ..
        }
    ));
    assert_eq!(state(&pool, ordinary).await?, "provider_accepted");
    assert_eq!(delivered.load(Ordering::Relaxed), 1);
    let proceed = |_| {
        delivered.fetch_add(1, Ordering::Relaxed);
        ready(None)
    };
    assert_eq!(
        database
            .dispatch_outbox_reply(destination(), claim, &[], proceed)
            .await?,
        ReplyDispatchResult::ClaimUnavailable
    );

    let generated = queue(&pool, owner, fixture_intent(owner, 1002), Some(INITIAL)).await?;
    let generated_claim = claim_for(&pool, &database, destination(), generated).await?;
    inbound(&pool, owner, Uuid::from_u128(302), 2).await?;
    assert_eq!(
        database
            .dispatch_outbox_reply(destination(), generated_claim, &[], proceed)
            .await?,
        ReplyDispatchResult::Cancelled
    );
    assert_eq!(state(&pool, generated).await?, "cancelled");

    let mut sensitive = fixture_intent(owner, 1003);
    sensitive["evidence_refs"] = json!([format!("definition:{DEFINITION}")]);
    let sensitive_outbox = queue(&pool, owner, sensitive, None).await?;
    sqlx::query(
        "INSERT INTO event_definitions(id,owner_id,namespace,key,status,current_version,revision)
         VALUES($1,$2,'user','user.synthetic_dispatch','active',1,1)",
    )
    .bind(DEFINITION)
    .bind(owner)
    .execute(&pool)
    .await?;
    sqlx::query(
        "INSERT INTO event_definition_versions(id,definition_id,version,schema,schema_hash,topology,
                field_metadata,labels,privacy,allowed_operations)
         VALUES(gen_random_uuid(),$1,1,'{}','synthetic','point','{}','{}','sensitive','[]')",
    ).bind(DEFINITION).execute(&pool).await?;
    let managers: Scopes = [Scope::ManageIntegrations].into_iter().collect();
    let grant_target = ShareDestination::new(DestinationKind::Channel, "fixture:primary")?;
    database
        .grant_tracker_share(
            managers,
            TrackerShareGrant {
                definition_id: DEFINITION,
                destination: grant_target,
                categories: &[ShareCategory::Schema, ShareCategory::Facts],
                granted_at: observed,
            },
        )
        .await?;
    let sensitive_claim = claim_for(&pool, &database, destination(), sensitive_outbox).await?;
    assert!(
        database
            .revoke_tracker_share(managers, DEFINITION, grant_target)
            .await?
    );
    assert_eq!(
        database
            .dispatch_outbox_reply(destination(), sensitive_claim, &[], proceed)
            .await?,
        ReplyDispatchResult::Cancelled
    );
    assert_eq!(state(&pool, sensitive_outbox).await?, "cancelled");

    let mut expired = fixture_intent(owner, 1004);
    expired["expires_at"] = json!("2026-01-01T00:00:00Z");
    let expired = queue(&pool, owner, expired, None).await?;
    let expired_claim = claim_for(&pool, &database, destination(), expired).await?;
    assert_eq!(
        database
            .dispatch_outbox_reply(destination(), expired_claim, &[], proceed)
            .await?,
        ReplyDispatchResult::Cancelled
    );
    assert_eq!(state(&pool, expired).await?, "expired");
    assert_eq!(delivered.load(Ordering::Relaxed), 1);

    let unknown = queue(&pool, owner, fixture_intent(owner, 1005), None).await?;
    let unknown_claim = claim_for(&pool, &database, destination(), unknown).await?;
    assert!(matches!(
        database
            .dispatch_outbox_reply(destination(), unknown_claim, &[], proceed)
            .await?,
        ReplyDispatchResult::Recorded {
            state: DeliveryState::Uncertain,
            ..
        }
    ));
    assert_eq!(state(&pool, unknown).await?, "uncertain");

    let failed_write = queue(&pool, owner, fixture_intent(owner, 1006), None).await?;
    let failed_claim = claim_for(&pool, &database, destination(), failed_write).await?;
    sqlx::query(
        "ALTER TABLE message_delivery_receipts ADD CHECK(detail<>'synthetic storage failure')",
    )
    .execute(&pool)
    .await?;
    let result = database
        .dispatch_outbox_reply(destination(), failed_claim, &[], |_| {
            delivered.fetch_add(1, Ordering::Relaxed);
            ready(Some(ReplyObservation {
                state: DeliveryState::Delivered,
                observed_at: observed,
                provider_reference: Some("opaque/synthetic-delivered".into()),
                detail: Some("synthetic storage failure".into()),
            }))
        })
        .await?;
    assert_eq!(result, ReplyDispatchResult::Uncertain);
    assert_eq!(state(&pool, failed_write).await?, "sending");
    assert_eq!(
        database
            .dispatch_outbox_reply(destination(), failed_claim, &[], proceed)
            .await?,
        ReplyDispatchResult::ClaimUnavailable
    );
    assert_eq!(delivered.load(Ordering::Relaxed), 3);
    let past_claim =
        StoredInstant::from_unix_microseconds(failed_claim.lease_until.unix_microseconds() + 1)?;
    assert_eq!(database.recover_expired_outbox(past_claim).await?, 1);
    assert_eq!(state(&pool, failed_write).await?, "uncertain");
    println!(
        "Verified stale-reply rejection, consent fencing, durable token rotation and uncertain-send recovery"
    );
    Ok(())
}

fn fixture_intent(owner: Uuid, id: u128) -> Value {
    json!({
        "intent_id": Uuid::from_u128(id).to_string(),
        "owner_id": owner.to_string(),
        "conversation_id": CONVERSATION.to_string(),
        "channel_instance": {"channel":"fixture","instance_id":"primary"},
        "blocks": [{"text":"synthetic outbox text"}],
    })
}

async fn clock(pool: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(pool)
        .await?;
    Ok(now.try_into()?)
}

async fn inbound(pool: &PgPool, owner: Uuid, id: Uuid, revision: i32) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,
                 external_event_id,sender_ref,received_at,kind,envelope,revision,status,operation_id)
         VALUES($1,$2,$3,'fixture','primary',$4,'synthetic-owner',now(),'text','{}',$5,'processed',$6)",
    ).bind(id).bind(owner).bind(CONVERSATION).bind(format!("synthetic:{revision}")).bind(revision)
        .bind(OPERATION).execute(pool).await?;
    Ok(())
}

async fn queue(
    pool: &PgPool,
    owner: Uuid,
    intent: Value,
    inbound: Option<Uuid>,
) -> Result<Uuid, Box<dyn Error>> {
    let id: Uuid = intent["intent_id"]
        .as_str()
        .ok_or("Missing synthetic intent")?
        .parse()?;
    let memory = inbound.map(|_| json!({"target_epoch":EPOCH.to_string(),"source_epochs":{}}));
    sqlx::query(
        "INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,dedup_key,intent,
                      memory_fence,inbound_message_id,state,attempts)
         VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,'queued',0)",
    )
    .bind(id)
    .bind(owner)
    .bind(CONVERSATION)
    .bind(OPERATION)
    .bind(format!("synthetic:{id}"))
    .bind(intent.to_string())
    .bind(memory.map(|value| value.to_string()))
    .bind(inbound)
    .execute(pool)
    .await?;
    Ok(id)
}

async fn claim_for(
    pool: &PgPool,
    database: &Database,
    destination: ReplyDestination<'_>,
    expected: Uuid,
) -> Result<ReplyClaim, Box<dyn Error>> {
    let claim = database
        .claim_outbox_reply(destination, clock(pool).await?, 120)
        .await?
        .ok_or("Expected due reply")?;
    assert_eq!(claim.outbox_id, expected);
    Ok(claim)
}

async fn state(pool: &PgPool, outbox_id: Uuid) -> Result<String, sqlx::Error> {
    sqlx::query_scalar("SELECT state FROM outbox_messages WHERE id=$1")
        .bind(outbox_id)
        .fetch_one(pool)
        .await
}
