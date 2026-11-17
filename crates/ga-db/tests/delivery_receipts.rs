use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    delivery::{
        DeliveryError, DeliveryReceipt, DeliveryState, DeliveryTarget, record_delivery_receipt,
    },
};
use ga_domain::{
    access::{Scope, Scopes},
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    initial: Value,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    target: Option<String>,
    lease: Option<String>,
    receipt: Option<Receipt>,
    recovery: Option<Recovery>,
    requeue: Option<Requeue>,
    outcome: Option<String>,
    snapshot: Option<Value>,
}

#[derive(Deserialize)]
struct Receipt {
    receipt_id: String,
    intent_id: String,
    state: DeliveryState,
    observed_at: String,
    provider_reference: Option<String>,
    detail: Option<String>,
}

#[derive(Deserialize)]
struct Recovery {
    clock: String,
    count: u64,
    snapshot: Value,
}

#[derive(Deserialize)]
struct Requeue {
    id: String,
    authorized: bool,
}

#[tokio::test]
#[ignore = "compares the native outbox to pinned Python on disposable TimescaleDB"]
async fn records_only_observed_progress_and_reconciles_uncertainty() -> Result<(), Box<dyn Error>> {
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
    let pool = PgPool::connect_with(options.clone()).await?;
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(current, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_DELIVERY_RECEIPTS")?)?)?;
    seed(&pool, owner, &fixture.initial).await?;
    assert_eq!(snapshot(&pool).await?, fixture.initial);

    let case_count = fixture.cases.len();
    let managers: Scopes = [Scope::ManageIntegrations].into_iter().collect();
    for case in fixture.cases {
        let expected = if let Some(recovery) = case.recovery {
            let clock = recovery.clock.parse()?;
            assert_eq!(
                database.recover_expired_outbox(clock).await?,
                recovery.count
            );
            assert_eq!(database.recover_expired_outbox(clock).await?, 0);
            recovery.snapshot
        } else {
            let result = if let Some(receipt) = case.receipt {
                let mut txn = pool.begin().await?;
                let outcome = record_delivery_receipt(
                    &mut txn,
                    DeliveryTarget {
                        owner_id: owner,
                        outbox_id: case.target.as_deref().ok_or("Missing intent ID")?.parse()?,
                        lease_token: case.lease.map(|value| value.parse()).transpose()?,
                    },
                    DeliveryReceipt {
                        id: receipt.receipt_id.parse()?,
                        intent_id: receipt.intent_id.parse()?,
                        state: receipt.state,
                        observed_at: receipt.observed_at.parse()?,
                        provider_reference: receipt.provider_reference.as_deref(),
                        detail: receipt.detail.as_deref(),
                    },
                )
                .await;
                txn.commit().await?;
                outcome.map(|id| id.to_string())
            } else {
                let requeue = case.requeue.ok_or("Missing requeue parameters")?;
                let scopes = if requeue.authorized {
                    managers
                } else {
                    Scopes::default()
                };
                database
                    .requeue_uncertain_delivery(scopes, owner, requeue.id.parse()?)
                    .await
                    .map(|()| "queued".to_owned())
            };
            let actual = match result {
                Ok(value) => value,
                Err(DeliveryError::LeaseReplaced) => "LeaseReplaced".into(),
                Err(DeliveryError::Forbidden) => "PermissionError".into(),
                Err(DeliveryError::RequiresReconciliation) => "Conflict".into(),
                Err(error) => return Err(format!("{}: {error}", case.name).into()),
            };
            assert_eq!(Some(actual), case.outcome, "{}", case.name);
            case.snapshot.ok_or("Missing paired outbox snapshot")?
        };
        assert_eq!(snapshot(&pool).await?, expected, "{}", case.name);
    }

    let target = DeliveryTarget {
        owner_id: owner,
        outbox_id: Uuid::from_u128(4),
        lease_token: None,
    };
    let later: StoredInstant = "2026-11-05T11:00:00Z".parse()?;
    assert_eq!(database.recover_expired_outbox(later).await?, 1);
    let mut txn = pool.begin().await?;
    for (number, state) in [(60, DeliveryState::Queued), (61, DeliveryState::Sending)] {
        record_delivery_receipt(
            &mut txn,
            target,
            DeliveryReceipt {
                id: Uuid::from_u128(number),
                intent_id: target.outbox_id,
                state,
                observed_at: later,
                provider_reference: None,
                detail: None,
            },
        )
        .await?;
    }
    let state: String = sqlx::query_scalar("SELECT state FROM outbox_messages WHERE id=$1")
        .bind(target.outbox_id)
        .fetch_one(&mut *txn)
        .await?;
    assert_eq!(state, "uncertain");
    txn.commit().await?;

    let previous = snapshot(&pool).await?;
    let receipt = || DeliveryReceipt {
        id: Uuid::from_u128(62),
        intent_id: target.outbox_id,
        state: DeliveryState::Read,
        observed_at: later,
        provider_reference: Some("opaque/read"),
        detail: Some("synthetic failure"),
    };
    let mut txn = pool.begin().await?;
    let foreign = DeliveryTarget {
        owner_id: Uuid::from_u128(63),
        ..target
    };
    assert!(matches!(
        record_delivery_receipt(&mut txn, foreign, receipt()).await,
        Err(DeliveryError::MissingIntent)
    ));
    txn.rollback().await?;
    assert!(matches!(
        database
            .requeue_uncertain_delivery(managers, foreign.owner_id, foreign.outbox_id)
            .await,
        Err(DeliveryError::MissingIntent)
    ));
    sqlx::query("ALTER TABLE message_delivery_receipts ADD CHECK(detail<>'synthetic failure')")
        .execute(&pool)
        .await?;
    let mut txn = pool.begin().await?;
    assert!(matches!(
        record_delivery_receipt(&mut txn, target, receipt()).await,
        Err(DeliveryError::Database)
    ));
    txn.rollback().await?;
    assert_eq!(snapshot(&pool).await?, previous);

    assert!(!DeliveryState::ProviderAccepted.confirms_delivery());
    assert!(DeliveryState::Delivered.confirms_delivery());
    assert!(!DeliveryState::Delivered.confirms_read());
    assert!(DeliveryState::Read.confirms_read());
    println!("{case_count} paired receipt and recovery snapshots matched");
    Ok(())
}

async fn seed(pool: &PgPool, owner: Uuid, initial: &Value) -> Result<(), sqlx::Error> {
    let conversation = Uuid::from_u128(101);
    sqlx::query(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
         VALUES($1,$2,'fixture','primary',gen_random_uuid(),'{}',false)",
    )
    .bind(conversation)
    .bind(owner)
    .execute(pool)
    .await?;
    sqlx::query(
        "INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,dedup_key,intent,state,
                attempts,provider_reference,lease_token,lease_until,next_attempt_at)
         SELECT id,$1,$2,gen_random_uuid(),concat('synthetic-receipt:',id),'{}',state,attempts,reference,
                lease_token,lease_until,next_attempt_at
         FROM jsonb_to_recordset($3::jsonb) AS r(id uuid,state text,attempts int,reference text,
              lease_token uuid,lease_until timestamptz,next_attempt_at timestamptz)",
    )
    .bind(owner)
    .bind(conversation)
    .bind(initial["outbox"].to_string())
    .execute(pool)
    .await?;
    Ok(())
}

async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let raw: String = sqlx::query_scalar(
        "SELECT jsonb_build_object(
            'outbox', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'id',id,'state',state,'attempts',attempts,'reference',provider_reference,
                'lease_token',lease_token,'lease_until',lease_until,'next_attempt_at',next_attempt_at
              ) ORDER BY id),'[]') FROM outbox_messages),
            'evidence', (SELECT coalesce(jsonb_agg(jsonb_build_array(
                id,outbox_message_id,state,observed_at,provider_reference,detail
              ) ORDER BY id),'[]') FROM message_delivery_receipts)
          )::text",
    )
    .fetch_one(pool)
    .await?;
    Ok(serde_json::from_str(&raw)?)
}
